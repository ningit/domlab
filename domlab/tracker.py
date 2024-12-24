#
# Track the event feeds of multiple contests
#

import asyncio
import concurrent.futures
import json
import logging
import threading

import codegavel
from codegavel import Toolchain
from .entities import Instance, Subject, SubmissionInfo

logger = logging.getLogger('domlab')


class SubjectTracker:
	"""Tracks the event feed of several subjects"""

	pool: concurrent.futures.ThreadPoolExecutor
	toolchain: Toolchain
	tasks: list[asyncio.Task]
	loop: asyncio.AbstractEventLoop

	def __init__(self, servers: dict[str, Instance], config: dict):
		# Servers with subjects to track
		self.servers = servers
		# Tasks that track each subject
		self.tasks = []

		# Add the event feeds to the selector
		self.loop = asyncio.get_event_loop()

		for server in servers.values():
			# Skip historic servers
			if server.historic:
				continue

			for subject in server.subjects.values():
				self.tasks.append(self.loop.create_task(self.track(subject),
				                                        name=f'tracker-{server.name}@{subject.name}'))

		# Pool of worker threads (if analyses are enabled)
		self.active = config.get('analyses', {}).get('active', True)
		self.pool, self.toolchain = None, None

		if self.active:
			max_workers = config.get('analyses', {}).get('workers', 1)
			self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
			                                                  thread_name_prefix='codegavel-runner')
			self.toolchain = Toolchain(compiler_args=config.get('analyses', {}).get('compiler_args', ('-DDOMJUDGE',)))

		# Listener (callbacks) for the events of the tracker
		self.listeners = []

		# Store some configuration options
		self.min_severity = config.get('analyses', {}).get('min_severity', 4)
		self.must_explain = config.get('analyses', {}).get('must_explain', False)

	def add_callback(self, cb):
		"""Add a callback to be informed of the events of the tracker"""

		self.listeners.append(cb)

	def _issue_event(self, **kwargs):
		"""Issue an event to all listeners"""

		for cb in self.listeners:
			cb(kwargs)

	async def _feed_judgement(self, subject: Subject, event: dict):
		"""Judgement received"""

		judg = await subject.add_judgement(event)
		subm_id = event['data']['submission_id']

		# The final judgement has a non-null end_time value
		if event['data'].get('end_time'):
			# We are running our own analyses (and this is not a rejudgement)
			if self.active and (subm := subject.submissions.get(subm_id)):
				self._issue_event(type='update', subject=subject, sid=subm_id, judgement=judg)

				# The first phase of our own analyses has already finished
				if subm.analysis_phase == 1:
					self.loop.create_task(self._analyze_phase2(subject, subm),
					                      name=f'phase2-{subm.sid}')

			else:
				self._issue_event(type='close-submission', subject=subject, sid=subm_id, judgement=judg)
				await subject.close_submission(subm_id)

	async def _feed_run(self, subject: Subject, event: dict):
		"""Run result received"""

		# Listeners are not informed of each run, but this may change in the future
		subject.add_run(event)

	async def _feed_submission(self, subject: Subject, event: dict):
		"""Submission received"""
		data = event['data']
		subm_id = data['id']

		if data.get('team_id') is None:
			return

		# Get the IP (at the current moment, not at the submission time)
		ip = await self._get_location(subject.instance, data['team_id'])

		# Obtain the source code for our analyses
		if self.active:
			await subject.download_source_code(subm_id)
			await subject.instance.download_problem(data['problem_id'])

		# Extract the other authors if required
		if subject.tag is not None:
			others = await self._get_other_author(subject, subm_id)
		else:
			others = None

		subm = subject.add_submission(event, ip=ip, others=others)

		# Create the task for our own analyses
		if subm and self.active:
			self.loop.create_task(self._analyze_phase1(subject, subm),
			                      name=f'phase1-{subm.sid}')

		self._issue_event(type='new-submission', subject=subject, submission=subm)

	async def _get_location(self, server: Instance, team_id: str):
		"""Get the location of the user by IP"""

		if user_info := server.get_user(team_id):
			return (await user_info)['last_ip']

	async def _get_other_author(self, subject: Subject, subm_id: str):
		"""Obtiene el segundo autor de un envío"""

		# Find the other author names
		users = set()

		# Use the already downloaded sources if they exist
		if self.active:
			source = (path.read_bytes() for path in (subject.workdir / subm_id / 'src').iterdir()
			          if path.suffix in ('.cpp', '.cc'))
		else:
			source = (content for name, content in (await subject.get_source_code(subm_id)).items()
			          if name.endswith('.cpp') or name.endswith('.cc'))

		for content in source:
			if users := {subject.tag.format(num.decode('ascii'))
			             for num in subject.tag_regex.findall(content)}:
				break

		return users

	def _do_analyze_phase1(self, subject: Subject, subm: SubmissionInfo):
		"""Analyze submissions (in other thread)"""

		subm_dir = subject.workdir / subm.sid

		analyses = self.toolchain.new_submission(
			source_dir=subm_dir / 'src',
			work_dir=subm_dir / 'work',
			output_dir=subm_dir / 'output',
		)

		analyses.check_static()
		analyses.check_custom()

		return analyses

	async def _analyze_phase1(self, subject: Subject, subm: SubmissionInfo):
		"""Analyze submissions (phase 1)"""

		subm.analyses = await self.loop.run_in_executor(self.pool, self._do_analyze_phase1, subject, subm)
		subm.analysis_phase = 1

		# If the submission already has a verdict
		if subm.judgement and subm.judgement.verdict:
			self.loop.create_task(self._analyze_phase2(subject, subm),
			                      name=f'phase2-{subm.sid}')

	def _do_analyze_phase2(self, subject: Subject, subm: SubmissionInfo):
		"""Analyze submissions (second phase)"""

		# Write the summary
		analyses: codegavel.Submission = subm.analyses

		# Directory of the problem (to find the testcases)
		problem_dir = subject.instance.workdir / 'problems' / subm.problem
		output_dir = subject.workdir / subm.sid / 'output'

		# Rerun the failed test cases with instrumentation
		for k, (verdict, _) in enumerate(subm.judgement.runs):
			if verdict in ('WA', 'RTE'):
				result = analyses.check_output(
					problem_dir / f'{k}.in',
					problem_dir / f'{k}.ans',
					output_dir / f'{k}.out',
					instrument=True,
					timelimit=1,
					memlimit=2097152,
					unit_name=threading.current_thread().name,
				)

				if result is None:
					logger.error(f'error when trying to run testcase {k + 1}'
					             f' of problem {subm.problem} on submission {subm.sid}')
					return False

				logger.info(f'run {subm.sid}/{k + 1} for problem {subm.problem} finished '
				            f'in {result.time / 1e9} seconds using {round(result.memory / 1e6)} Mb '
							f'with verdict {result.verdict.name} ({verdict} expected)')

		# Write the summary
		summary = analyses.summary(verdicts={v for v, _ in subm.judgement.runs},
		                           min_severity=self.min_severity,
		                           must_explain=self.must_explain)

		with (analyses.output_dir / 'summary.json').open('w') as out:
			json.dump(summary, out)

		return len(summary) > 0

	async def _analyze_phase2(self, subject: Subject, subm: SubmissionInfo):
		"""Phase 2 has finished"""

		has_issues = await self.loop.run_in_executor(self.pool, self._do_analyze_phase2, subject, subm)
		subm.analysis_phase = 3 if has_issues else 2

		self._issue_event(type='close-submission', subject=subject, sid=subm.sid, judgement=subm.judgement)
		await subject.close_submission(subm.sid)

	async def track(self, subject):
		"""Track events from the feed"""

		# Event handlers in the class
		event_handlers = {
			# 'clarification': self._feed_clarification,
			'judgements': self._feed_judgement,
			'submissions': self._feed_submission,
			'runs': self._feed_run,
		}

		# Iterate asynchronously on events
		async for event in subject.listen():
			if cb := event_handlers.get(event['type']):
				await cb(subject, event)

		logger.warning(f'the event feed for {subject.name} has finished')
