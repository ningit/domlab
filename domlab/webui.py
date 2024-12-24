#
# Web interface (essentially a REST API interface)
#

import asyncio
import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

import tornado.web
import tornado.websocket
from motor.motor_asyncio import AsyncIOMotorClient
from motor.motor_asyncio import AsyncIOMotorDatabase as Database
from pymongo.errors import ServerSelectionTimeoutError
from tornado.netutil import bind_unix_socket

from .entities import Instance
from .tracker import SubjectTracker

logger = logging.getLogger('domlab')


class HomeHandler(tornado.web.RequestHandler):
	"""Start screen of the web interface"""

	application: 'DOMlab'

	def get(self):
		self.write({'subjects': [
			{
				'subject': subject.name,
				'server': server.name,
				'server_url': server.url,
			}
			for server in self.application.servers.values()
			for subject in server.subjects.values()
		]})


class DiagnosticHandler(tornado.web.RequestHandler):
	"""Diagnostic handler"""

	application: 'DOMlab'

	async def post(self):
		# Check required arguments
		for argument in ('server', 'subject', 'sid', 'timestamp'):
			if self.get_argument(argument, None) is None:
				self.set_status(400)
				self.write({'ok': False, 'reason': f'missing required argument: {argument}'})
				return

		# Look for the server
		server = self.get_argument('server')

		if (server_obj := self.application.servers.get(server)) is None:
			self.set_status(400)
			self.write({'ok': False, 'reason': f'unknown server: {server}'})
			return

		# Look for the subject
		subject = self.get_argument('subject')

		if (subject_obj := server_obj.get(subject)) is None:
			self.set_status(400)
			self.write({'ok': False, 'reason': f'unknown subject: {subject}'})
			return

		# Look for the submission
		sid = self.get_argument('sid')
		subm = await subject_obj.get_submission(sid)

		# Check submission date
		if subm is None or not self._check_dates(subm['time'], datetime.fromtimestamp(float(self.get_argument('timestamp')))):
			self.set_status(403)
			self.write({'ok': False, 'reason': f'unknown submission {sid} or access not allowed'})
			return

		# Check whether there is an already cached HTML document
		if (html := subm.get('cached_html')) is None:
			status, html = subject_obj.make_advice(sid)
			if status == 404:
				self.set_status(404)
				self.write({'ok': False, 'reason': f'no advice is available yet for {sid}'})
				return
			self.set_status(status)

		self.set_header('Content-Type', 'text/html; charset=UTF-8')
		self.write(html)

	def _check_dates(self, date1: datetime, date2: datetime):
		"""Check whether two dates match at the required level"""

		the_same = date1.hour == date2.hour and date1.minute == date2.minute and date1.second == date2.second

		if not the_same:
			logger.warning(f'diagnostic request rejected due to non-matching dates {date1} vs. {date2}')

		return the_same


class SubmissionFeedSocket(tornado.websocket.WebSocketHandler):
	"""Event feed from our own information"""

	application: 'DOMlab'

	class State(Enum):
		IDLE = 1  # no submission message has been received yet
		HISTORY = 2  # dumping submission history
		TRANSITION = 3  # transition from history to active state
		ACTIVE = 4  # dumping live submissions
		INACTIVE = 5  # the socket is exhausted

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.submission_index = None
		self.pending_updates = set()
		self.state = self.State.IDLE

		# Closure date, to close the websocket
		self.until = None

		# Submission not yet sent to the client
		self.pending_submissions = {}
		self.subject = None

		# Event for transitioning from history to active state
		self.to_active = asyncio.Event()

	def check_origin(self, origin):
		# Allow any origin for the moment
		return True

	def open(self):
		logger.info(f'opened websocket connection from {self.request.remote_ip}')

	def _check_date(self, name: str, date: int):
		"""Check whether a date is valid a convert it to an internal date"""

		try:
			return datetime.fromtimestamp(date)

		except ValueError:
			self.write_message({'type': 'error', 'reason': f'wrong timestamp in {name} argument: {date}'})

	async def on_message(self, message):
		"""Recibe a message to initiate the subscription"""

		# Ignore messages when not in the idle state
		if self.state != self.State.IDLE:
			return

		# Message with the information of the subject to track
		try:
			message = json.loads(message)

		except json.JSONDecodeError:
			self.close(1003, 'wrong JSON payload')
			return

		# Check arguments
		for argument in ('server', 'subject'):
			if argument not in message:
				await self.write_message({'type': 'error', 'reason': f'missing required argument: {argument}'})
				return

		# Mandatory arguments
		server = message['server']
		subject = message['subject']

		# TODO: Consider token authentication to the API,
		# but probably through the Authentication header
		# token = message['token']

		last_submission = message.get('last_submission')
		since = message.get('since')
		until = message.get('until')

		# Check whether dates are valid dates
		if (since and (since := self._check_date('since', since)) is None) or \
		   (until and (until := self._check_date('until', until)) is None):
			return

		# Closure time for this websocket
		self.until = until + timedelta(minutes=5) if until else None

		# TODO: Consider shotting this down when until is reached
		# self.application.loop.call_at()

		# No submissions in the empty time period
		if until and since and since >= until:
			self.close(1000, 'empty date range')
			self.state = self.State.INACTIVE
			return

		# Look for the server
		if (server_obj := self.application.servers.get(server)) is None:
			await self.write_message({'type': 'error', 'reason': f'unknown server: {server}'})
			return

		# Look for the subject
		if (subject_obj := server_obj.get(subject)) is None:
			await self.write_message({'type': 'error', 'reason': f'unknown subject: {subject}'})
			return

		self.subject = subject_obj

		# Change to history state
		self.state = self.State.HISTORY
		self.application.websockets.append(self)

		# Copy the pending submissions before dumping the history
		self.pending_submissions = subject_obj.submissions.copy()

		# Obtain all submissions from the history
		async for subm in subject_obj.get_submissions(since, until, last_submission):
			await self._write_submission(subm)

		# Send also the submissions notified after the query (if any)
		# (we add a lock to the submission dictionary during this)
		self.to_active.clear()
		self.state = self.State.TRANSITION

		for subm in self.pending_submissions.values():
			await self._write_submission(subm.to_json())

		self.pending_submissions.clear()

		# Change to active state
		self.state = self.State.ACTIVE
		self.to_active.set()

	def _make_student_info(self, team: str):
		"""Make a dictionary describing a team"""
		team_info = self.subject.instance.get_student(team)

		return {
			'team_id': team,
			'name': team_info.display_name if team_info else f'Desconocido ({team})',
		}

	def _write_submission(self, subm: dict):
		"""Write a submission to the web socket"""

		submitter = subm['team']

		return self.write_message({
			'type': 'submission',
			'sid': subm['sid'],
			'time': subm['time'].isoformat(),
			'submitter': self._make_student_info(submitter),
			'problem': subm['problem'],
			'ip': subm['ip'],
			'other': [self._make_student_info(team) for team in (subm['other_authors'] or ()) if team != submitter],
			'judgement': subm['judgement'],
		})

	def on_close(self):
		if self.state != self.State.IDLE:
			self.application.websockets.remove(self)
		logger.info(f'closed websocket connection from {self.request.remote_ip}')

	async def handle_event(self, event: dict):
		"""Handle event"""

		mtype = event['type']
		subject = event['subject']

		# Wait when in transition state
		if self.state == self.State.TRANSITION:
			await self.to_active.wait()

		# Identity of objects, we are in the same memory space
		if subject is self.subject:
			match mtype:
				# New submission
				case 'new-submission':
					# If received while sending the history, we store it
					subm = event['submission']

					if self.state == self.State.HISTORY:
						self.pending_submissions[subm.sid] = subm
					else:
						await self._write_submission(subm.to_json())

				case 'update' | 'close-submission':
					judg = event['judgement']
					sid = event['sid']

					if self.state == self.State.ACTIVE:
						await self.write_message({
							'type': 'update',
							'sid': sid,
							**judg.to_json(),
						})


class DOMlab(tornado.web.Application):
	"""Web interface for following DOMjudge submissions"""

	HANDLERS = [
		('/api/feed', SubmissionFeedSocket),
		('/api/home', HomeHandler),
		('/api/diagnostic', DiagnosticHandler),
	]

	servers: dict[str, Instance]
	websockets: list[SubmissionFeedSocket]
	database: Database

	def __init__(self, config: dict):
		super().__init__(self.HANDLERS)

		# Configuration
		self.config = config
		# Database URL
		self.database = None

		# Initialize the working directory
		self.workdir = Path(config.get('general', {}).get('workdir', 'domlab_workdir'))
		self.workdir.mkdir(mode=0o771, parents=True, exist_ok=True)

		# Servers and subjects being followed
		self.servers = {}

		# Active websockets
		self.websockets = []

		# Event loop
		self.loop = None
		# Subject feed tracker
		self.tracker = None

	async def load(self):
		"""Load databases, servers and subjects"""

		# This could be done synchronously, because the server has
		# not started yet, but we use asynchronous clients

		if not await self._load_db():
			return False

		self.loop = asyncio.get_running_loop()

		for name, info in self.config.get('servers', {}).items():
			server = Instance(name, info, self.workdir / name)

			await server.connect(self.database)

			if server.subjects:
				self.servers[name] = server
			else:
				logger.error(f'no valid subject in {name} server, it will be ignored')

		if not self.servers:
			logger.fatal('no servers to track, we stop')
			return False

		# Tracker
		self.tracker = SubjectTracker(self.servers, self.config)
		self.tracker.add_callback(self._tracker_callback)

		return True

	async def _load_db(self):
		"""Load database"""

		database_url = self.config.get('database', {}).get('url', 'mongodb://localhost')
		logger.info(f'trying to connect to the MongoDB database {database_url}')
		mongo_client = AsyncIOMotorClient(database_url)

		try:
			mongo_info = await mongo_client.server_info()
			logger.info(f'connected to MongoDB version {mongo_info["version"]}')

		except ServerSelectionTimeoutError:
			logger.fatal(f'cannot connect to MongoDB database: {database_url}')
			return False

		# Get the database in the URL or otherwise domlab
		self.database = mongo_client.get_default_database(default='domlab')

		# Set the index for the metadata collection
		if not self.database.subject_metadata.index_information():
			await self.database.subject_metadata.create_index(('instance', 'subject'),
			                                                  name='metadata-key',
			                                                  unique=True)

		return True

	def _tracker_callback(self, event: dict):
		"""Callback for events from the tracker"""

		self.loop.create_task(self._handle_tracker_event(event), name='tracker-callback')

	async def _handle_tracker_event(self, event: dict):
		"""Handle and event from the tracker"""

		# Offer the event to the websocket handlers
		# (sequentially, so this may be a bottleneck)
		for websocket in self.websockets:
			await websocket.handle_event(event)

	async def main(self, url):
		# Set an exception handler
		# asyncio.get_running_loop().set_exception_handler(silent_exception_handler)

		server = tornado.web.HTTPServer(self)
		# Listen either on a TCP port or a Unix socket
		if isinstance(url, tuple):
			address, port = url
			server.bind(port, address)

		else:
			socket = bind_unix_socket(url, mode=0o606)
			server.add_socket(socket)

		# Load everything required
		if not await self.load():
			return 3

		server.start()
		await asyncio.Event().wait()
		return 0
