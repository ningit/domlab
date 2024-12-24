#
# Class to communicate asynchronously with DOMjudge
#

import base64
import json
import logging

import httpx
import lxml.html

logger = logging.getLogger('domlab')


class JudgeServer:
	"""Connection to a DOMjudge instance"""

	def __init__(self, url: str, user: str, passwd: str):
		# Session for the API
		self.api_session = httpx.AsyncClient(follow_redirects=True)
		# Session for the web client
		self.web_session = httpx.AsyncClient(follow_redirects=True)
		# Base URL
		self.url = url.rstrip('/')

		# Username and password
		self.user, self.passwd = user, passwd

		self.api_session.auth = (user, passwd.encode())

	async def connect(self, name: str):
		"""Connect with the API and the web interfaces"""

		# Check that the user have enough permissions
		try:
			answer = await self.ask('user')

		except httpx.ConnectTimeout:
			logger.error(f'timeout when trying to connect with {name} server')
			return False

		except httpx.ConnectError as e:
			logger.error(f'error when trying to connect with {name} server: {e}')
			return False

		# It should have the admin or jury role
		# (api_reader is enough for API operations, but jury is required for the web)
		roles = answer.get('roles', ())

		if not any(role in roles for role in ('admin', 'jury')):
			logger.error(f'error with {name} server: user {answer["username"]} does not have enough permissions')
			return False

		return await self.connect_web()

	async def connect_web(self):
		"""Connect with the web interfaces"""

		# Log into the web interface
		login_page = await self.web_session.get(f'{self.url}/login')

		if login_page.status_code != 200:
			raise ValueError(f'cannot access login URL: {login_page.status_code} {login_page.reason_phrase}')

		login_page = lxml.html.document_fromstring(login_page.text)
		login_form = login_page.forms[0]

		login_form.inputs['_username'].value = self.user
		login_form.inputs['_password'].value = self.passwd

		def login_submit(method, url, values):
			return self.web_session.post(f'{self.url}/login', data=dict(values))

		response = await lxml.html.submit_form(login_form, open_http=login_submit)

		# We will only be redirected to a different page if login was successful
		return not response.url.path.endswith('login')

	async def ask(self, method: str):
		"""Call to the given method"""

		return (await self.api_session.get(f'{self.url}/api/v4/{method}')).json()

	def get_user(self, user_id: str):
		"""Get information about the given user"""

		return self.ask(f'users/{user_id}')

	async def get_contest(self, cid: str):
		"""Get a contest from the judge"""

		answer = await self.api_session.get(f'{self.url}/api/v4/contests/{cid}')

		if answer.status_code == 200:
			return Contest(self, answer.json())

	async def download_problem(self, problem_id: str, out_file):
		"""Download a problem as a ZIP file"""

		# Turn the problem external id into an internal ID
		problem_list = await self.web_session.get(f'{self.url}/jury/problems')

		# The credential may have expired, so we try to reconnect
		if problem_list.url.path.endswith('login'):
			await self.connect_web()
			problem_list = await self.web_session.get(f'{self.url}/jury/problems')

		# Look for the external ID in the problem list
		internal_id = None

		for row in lxml.html.document_fromstring(problem_list.text).xpath('//tr'):
			if len(row) > 2 and row[1].tag == 'td' and row[1][0].text.strip() == problem_id:
				internal_id = row[0][0].text.strip()
				break

		# Copy the ZIP to the given file-like object without storing it completely in memory
		async with self.web_session.stream('GET', f'{self.url}/jury/problems/{internal_id}/export', timeout=60) as source:
			async for chunk in source.aiter_bytes():
				out_file.write(chunk)


class Contest:
	"""DOMjudge contest"""

	def __init__(self, server: JudgeServer, data: dict):
		self.server = server

		self.cid = data.get('cid')
		self.name = data['name']
		self.external_id = data['id']

	async def get_submission_code(self, subm_id: str):
		"""Get the source code of a submission"""

		return {unit['filename']: base64.b64decode(unit['source'])
                        for unit in await self.ask(f'submissions/{subm_id}/source-code')}

	async def listen(self, from_token=None, types=()):
		"""Listen to the event feed and iterate over messages"""

		url = f'{self.server.url}/api/v4/contests/{self.external_id}/event-feed'
		params = {'stream': 'true'}

		# Initial token (to avoid receiving old events)
		if from_token is not None:
			params['since_token'] = from_token

		# Event type filter
		if types:
			params['types'] = ','.join(params)

		async with self.server.api_session.stream('GET', url, params=params, timeout=None) as feed:
			# Check whether this has worked
			if feed.status_code != 200:
				logger.error(f'cannot connect to the event feed for contest {self.external_id}')
				return

			# Iterate over each line and parse its JSON
			async for line in feed.aiter_lines():
				if line:
					yield json.loads(line)

	def ask(self, method: str):
		"""Call to a given method on the contest"""

		return self.server.ask(f'contests/{self.external_id}/{method}')

