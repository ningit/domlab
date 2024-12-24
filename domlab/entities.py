#
# Entities involved in the task
#

import json
import logging
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorDatabase as Database

from .domjudge import JudgeServer
from .summary import make_summary

logger = logging.getLogger('domlab')


class Student:
	"""Student in the DOMjudge instance"""

	def __init__(self, user_id, display_name=None):
		self.user_id = user_id
		self.display_name = display_name

	def __repr__(self):
		return f'Student({self.user_id}, {self.display_name})'


class Instance:
	"""DOMjudge instance"""

	subjects: dict[str, 'Subject']

	def __init__(self, name: str, data: dict, workdir: Path):
		# Name used to designate this server in this tool
		self.name = name
		# URL and login information for the server
		self.url, user, passwd = data['url'], data['user'], data['pass']

		# Object to communicate with the DOMjudge instance
		self.judge = JudgeServer(self.url, user, passwd)
		# Dictionary with the subject objects belonging to this instance
		self.subjects = data.get('subjects', {})
		# Work directory for this server (with subfolders problems, contests, etc.)
		self.workdir = workdir
		# Map from team names to user information
		self.student_map = {}

		# The instance can work in historic or offline mode, where only historic
		# data from the database is available
		self.historic = True

	async def connect(self, database: Database):
		"""Connect with the DOMjudge instance and load its subjects"""

		# Check whether the server is online and reachable
		self.historic = not await self.judge.connect(self.name)

		if self.historic:
			logger.warning(f'running {self.name} server in historic mode')

		# Load subjects
		subject_spec = self.subjects
		self.subjects = {}

		for name, subject_info in subject_spec.items():
			subject_wd = self.workdir / 'contests' / name

			subject = Subject(self, subject_wd, database, name)

			# Load the subject from the server and database
			if not self.historic and not (await subject.load(database, subject_info)):
				logger.error(f'cannot access subject {name} at {self.name}')

			if not self.historic or await subject.has_history():
				self.subjects[name] = subject

		# Get the student map to translate team names to usernames and display names
		if not self.historic:
			await self._get_student_map()

	def get_student(self, team_id: str) -> Student | None:
		"""Get student information for a given team id"""

		# In historic mode, the student map cannont be built, so we use the team ID
		if self.historic:
			return Student(None, team_id)

		return self.student_map.get(team_id)

	def get(self, name: str) -> 'Subject | None':
		"""Get a subject in this instance"""
		return self.subjects.get(name)

	async def download_problem(self, problem_id: str, replace=False):
		"""Download a problem to the workspace"""

		problem_dir = self.workdir / 'problems' / problem_id

		# Check whether the entry already exists
		if problem_dir.exists():
			if replace:
				shutil.rmtree(problem_dir)
			else:
				return

		# Download the problem files
		problem_dir.mkdir(parents=True)

		with tempfile.TemporaryFile(suffix='.zip') as tmpfile:
			await self.judge.download_problem(problem_id, tmpfile)

			# This is perhaps a time-consuming sequential operations,
			# but it should not happen quite often
			try:
				with zipfile.ZipFile(tmpfile) as zipf:
					data = zipfile.Path(zipf, at='data')
					start, count = 0, 0

					# Sample test cases are numbered before secret ones
					for kind in ('sample', 'secret'):
						if (data / kind).exists():
							for path in (data / kind).iterdir():
								target_path = problem_dir / f'{int(path.stem) - 1 + start}{path.suffix}'

								if path.suffix == '.in':
									count += 1

								with path.open('rb') as source, target_path.open('wb') as target:
									shutil.copyfileobj(source, target)

							start = count
			except Exception as e:
				logger.error(e)

	def get_user(self, team_id: str):
		"""Get information for a given user"""

		if student := self.student_map.get(team_id):
			return self.judge.get_user(student.user_id)

	async def _get_student_map(self):
		"""Obtain a map from team names to display names and usernames"""

		# We obtain at the initialization time to have it updated, but
		# we could also store it in the database or the filesystem
		self.student_map = {user['team_id']: Student(user['id'])
		                    for user in await self.judge.ask('users')
		                    if user['team_id'] is not None}

		# Add the display name to the user map
		for team in await self.judge.ask('teams'):
			if student := self.student_map.get(team['id']):
				student.display_name = team['display_name'] or team['name']


class SubmissionInfo:
	"""Submission information"""
	def __init__(self, data, ip=None, others=None):
		# Submission time
		self.time = datetime.fromisoformat(data['time'])
		# Internal identifier in DOMjudge
		self.sid = data['id']
		# Team identifier in DOMjudge
		self.team = data['team_id']
		# Problem identifier in DOMjudge
		self.problem = data['problem_id']
		# Last judgement for this submission
		self.judgement = None
		# IP address of the user when the submission was done
		self.ip = ip
		# Other authors according to their tags
		self.other_authors = others

		# Results of our own analyses
		self.analyses = None
		self.analysis_phase = 0

	def add_judgement(self, judg):
		"""Update the judgement for this submission"""
		self.judgement = judg

	def done(self):
		"""Whether the submission has been solved"""
		return self.judgement and self.judgement.verdict

	def to_json(self):
		"""Dump the submission as a JSON document"""

		return {
			'sid': self.sid,
			'time': self.time,
			'team': self.team,
			'problem': self.problem,
			'ip': self.ip,
			'other_authors': list(self.other_authors) if self.other_authors is not None else None,
			'judgement': self.judgement.to_json() if self.judgement else None,
			'issues': (self.analysis_phase == 3) if self.analysis_phase >= 2 else None,
		}


class JudgementInfo:
	"""Judgement information"""

	def __init__(self, data: dict, subm: SubmissionInfo | None = None):
		self.jid = data['id']
		self.verdict = None
		self.runs = []

		# Submission information (not need, but could be convenient)
		self.subm = subm

	def update(self, data: dict):
		"""Update the verdict (handle an update event)"""

		self.verdict = data['judgement_type_id']

	def add_run(self, data):
		"""Add a run to the judgement"""

		verdict = data['judgement_type_id']
		ordinal = data['ordinal'] - 1
		run_time = data['run_time']

		# They might come out of order
		if ordinal >= len(self.runs):
			self.runs.extend((None, ) * (ordinal - len(self.runs) + 1))

		self.runs[ordinal] = (verdict, run_time)

	def to_json(self):
		"""Dump the judgment as a JSON document"""

		return {
			'verdict': self.verdict,
			'runs': self.runs,
		}


class Subject:
	"""Subject in the judge to be followed"""

	def __init__(self, instance: Instance, workdir: Path, database: Database, name: str):
		# Name used in this tool
		self.name = name
		# DOMjudge server this subject belongs to
		self.instance = instance
		# Working directory for submissions
		self.workdir = workdir
		# Whether in historic mode
		self.historic = True

		# Some attributes that are not used in historic mode
		self.contest, self.tag, self.tag_regex = None, None, None
		self.database_db, self.last_event, self.last_submission = None, None, None

		# Collection object for this subject from the database client
		self.collection = database.get_collection(f'@{instance.name}:{name}')

		# Pending submission and judgements
		self.submissions = {}
		self.judgements = {}

	async def load(self, database: Database, data: dict):
		"""Load subject by connecting to the server and database"""

		# Object to communicate with the DOMjudge contest
		self.contest = await self.instance.judge.get_contest(data['cid'])

		if self.contest is not None:
			self.historic = False
		else:
			return False

		# Tag for detecting additional users (if enabled)
		if tag := data.get('tag'):
			# If tag is a string, we take it as the prefix
			if isinstance(tag, str):
				prefix = tag

			else:
				if (prefix := tag.get('prefix')) is None:
					logger.error(f'tag dictionary for subject {self.name} does not contain a prefix field, using the subject name')
					prefix = self.name

				self.tag = tag.get('canonical')

			# If canonical is not given, we assume the usual format with 2 digits
			if self.tag is None:
				self.tag = f'{prefix}{{:0>2}}'

			self.tag_regex = re.compile(f'{prefix}[ ]*(\\d+)'.encode())

		# Create the workspace in the filesystem to store submissions
		self.workdir.mkdir(parents=True, exist_ok=True)

		await self._load_from_database(database, from_event=data.get('from_event'))

		return True

	async def _load_from_database(self, database, from_event=None):
		"""Load information from the database"""

		# If the collection is created for the first time, set up its indices
		if len(self.collection.index_information()) <= 1:
			await self.collection.create_index('sid', name='sid-index', unique=True)
			await self.collection.create_index('time', name='time-index')

		# Metadata collection (to store the last seen event)
		self.metadata_db = database.get_collection('subject_metadata')

		# Obtain the token of the last seen event (otherwise the feed must be read
		# from the beginning of time)
		last_event = await anext(self.metadata_db.find({'instance': self.instance.name, 'subject': self.name},
		                                               {'last_event': 1}), None)

		if last_event is None:
			self.last_event = from_event  # next event
		else:
			self.last_event = last_event['last_event']

		# The identifier of the last submission (if any, to detect rejudgings)
		if (last_submission := await anext(self.collection.aggregate([
			{'$sort': {'sid': -1}}, {'$limit': 1}]), None)) is not None:
			self.last_submission = int(last_submission['sid'])

	async def has_history(self):
		"""Whether the subject history has some content"""

		return await self.collection.count_documents({}) > 0

	def listen(self):
		"""Obtain the event feed"""

		# Only makes sense in online mode
		assert self.contest is not None

		return self.contest.listen(from_token=self.last_event)

	def get_source_code(self, sid: str):
		"""Download submission code"""

		return self.contest.get_submission_code(sid)

	async def download_source_code(self, sid: str):
		"""Download submission to the workspace"""

		# Make a directory for the submission sources
		subm_dir = self.workdir / sid / 'src'
		subm_dir.mkdir(exist_ok=True, parents=True)

		for name, content in (await self.get_source_code(sid)).items():
			# Skip (silently) files with slashes
			if '/' not in name:
				# This is costly
				(subm_dir / name).write_bytes(content)

	def add_submission(self, event, ip=None, others=None) -> SubmissionInfo | None:
		"""Handle a new submission"""

		sid = event['data']['id']

		# DOMjudge sends submission events when rejudging and also for completing
		# some fields of the submission description (entry point), but we ignore them
		if (self.last_submission is not None and int(sid) <= self.last_submission) \
		   or sid in self.submissions:
			return None

		# Keep the last submission index to detect rejudgings
		self.last_submission = int(sid)

		# Register the submission
		subm = SubmissionInfo(event['data'], ip=ip, others=others)
		self.submissions[subm.sid] = subm

		logger.debug(f'processing submission with ID {subm.sid} to problem {subm.problem} by {subm.team}')

		return subm

	async def add_judgement(self, event):
		"""Handle a new judgement"""

		jid = event['data']['id']
		sid = event['data']['submission_id']

		judg, subm = None, self.submissions.get(sid)

		# Judgement events are received when the judgements start (not always when rejudging)
		# and when it finishes (end_time can be used to distinguish between them).
		if not event['data'].get('end_time'):
			# Start events sometimes come twice
			if jid not in self.judgements:
				judg = JudgementInfo(event['data'], subm)
				self.judgements[judg.jid] = judg

				if subm is not None:
					subm.add_judgement(judg)
				else:
					logger.debug(f'started judgement {jid} for inactive submission {sid}')

		else:
			# Look for the judgement (created by the start message or the run events)
			if judg := self.judgements.get(jid):
				# Remove the judgement from the dictionary
				self.judgements.pop(judg.jid)

			else:
				logger.debug(f'judgement {jid} comes without being announced, there are no runs for it')
				judg = JudgementInfo(event['data'], None)

			# Update the verdict
			judg.update(event['data'])

			# Check whether the submission in an active one
			if subm is None:
				logger.debug(f'judgement {jid} for an inactive submission {sid}, probably a rejudging')
				# This update may fail if submission is not in the history, but we do not care
				await self.collection.update_one({'sid': sid}, {'$set': {'judgement': judg.to_json()}})

			else:
				subm.add_judgement(judg)

			# Set last event token
			await self.metadata_db.update_one({'instance': self.instance.name, 'subject': self.name},
			                                  {'$set': {'last_event': int(event['token'])}}, upsert=True)

		return judg

	def add_run(self, event):
		"""Handle a new run"""

		jid = event['data']['judgement_id']

		# In case of rejudging, some runs come without being announced with a judgement creation event
		if (judg := self.judgements.get(jid)) is None:
			logger.debug(f'run {event["data"]["id"]} references unknown judgement {jid}, creating it')

			judg = JudgementInfo(dict(id=jid), None)
			self.judgements[judg.jid] = judg

		judg.add_run(event['data'])

	async def close_submission(self, sid):
		"""Close and archive a submission"""

		if sid in self.submissions:
			submission = self.submissions.pop(sid)

			await self.collection.insert_one(submission.to_json())
			logger.debug(f'closing submission with ID {submission.sid}')

	async def get_submission(self, sid):
		"""Get a submission (closed or not)"""

		# Get an open submission
		if subm := self.submissions.get(sid):
			return subm.to_json()

		# Otherwise, get the submission from the database
		return await anext(self.collection.find({'sid': sid}), None)

	def get_submissions(self, since=None, until=None, last_submission=None):
		"""Get all submissions satisfying the given conditions"""

		conditions = {}

		if since:
			conditions['time'] = {'$gte': since}

		if until:
			conditions.setdefault('time', {})['$lt'] = until

		if last_submission:
			conditions['sid'] = {'$gt': last_submission}

		return self.collection.find(conditions).sort('sid')

	def make_advice(self, sid):
		"""Make the advice for a submission"""

		# Summary path
		summary_path = self.workdir / sid / 'output' / 'summary.json'

		# HTTP status codes are used to describe why an advice is not available
		if not summary_path.exists():
			return 404, None

		with summary_path.open() as summary_file:
			summary = json.load(summary_file)

		if not summary:
			return 204, ''

		return 200, make_summary(summary)
