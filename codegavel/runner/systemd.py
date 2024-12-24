#
# Runner using systemd through pystemd
#

import contextlib
import tempfile

import pystemd
import pystemd.run as sd_run

from .common import RunStatus, CompletedRun
from ..util import SyslogReader


class SystemdRunner:
	"""Confined runner using systemd through pystemd"""

	SANDBOX_SETTINGS = {
		'Description': 'codegavel runner service',
		'PrivateUsers': 'true',
		'ProtectSystem': 'strict',
		'ProtectHome': 'read-only',
		'ReadOnlyPaths': '/',
		'PrivateTmp': 'true',
		'PrivateDevices': 'true',
		'PrivateNetwork': 'true',
		'_custom': (b'AddRef', b'b', True),
	}

	def __init__(self, unit_name='codegavel-runner.service'):
		self.unit_name = unit_name
		self.journal = SyslogReader()

	def _stop_service(self):
		"""Stop the service if running"""

		try:
			with pystemd.dbuslib.DBus(user_mode=True) as bus, \
			     pystemd.systemd1.Unit(self.unit_name, bus=bus) as unit:
				unit.Stop(b'fail')
		except:
			pass  # not running

	def __call__(self, cmd, basedir=None, stdin=None, stdout=None, stderr=None, memlimit=None, timeout=None,
	             write_dirs=(), filesize_limit=10000000, task_limit=None, env=None, unit_name=None):
		"""Execute the given program"""

		# Set up options
		settings = self.SANDBOX_SETTINGS.copy()

		# Memory limit
		if memlimit is not None:
			settings['MemoryMax'] = memlimit * 1000000
			settings['MemorySwapMax'] = 0

		# File size limit
		if filesize_limit is not None:
			settings['LimitFSIZE'] = filesize_limit

		# Limit for the number of processes
		if task_limit is not None:
			settings['TasksMax'] = task_limit

		# Write directories
		if write_dirs:
			settings['ReadWritePaths'] = write_dirs

		# Standard error output
		if stderr is None:
			stderr_context = tempfile.TemporaryFile()
		else:
			stderr_context = contextlib.nullcontext(stderr)

		# Unit name (may be overwritten for parallel executions)
		unit_name = f'{unit_name}.service' if unit_name else self.unit_name

		with stderr_context as tmp_file:
			try:
				unit = sd_run(cmd, name=unit_name,
				              wait=True, remain_after_exit=True,
				              user_mode=True, cwd=basedir,
				              stdin=stdin, stdout=stdout, stderr=tmp_file,
				              extra=settings, runtime_max_sec=timeout, env=env)

			# Stop the service if the wait is interrupted
			except KeyboardInterrupt:
				self._stop_service()
				raise

			# Get standard error contents
			if stderr is None:
				tmp_file.flush()
				tmp_file.seek(0)
				stderr_bytes = tmp_file.read()
			else:
				stderr_bytes = None

		cpu_time = unit.Service.CPUUsageNSec
		mem_peak = unit.Service.MemoryPeak  # systemd 256.3+ is required
		status = RunStatus.OK

		# Check whether it has failed and why
		if unit.Unit.SubState == b'failed':
			result = unit.Service.Result

			if result == b'timeout':
				status = RunStatus.TIMEOUT
			elif result == b'oom-kill':
				status = RunStatus.OOM
			else:
				status = RunStatus.ERROR

			unit.Unit.ResetFailed()
		else:
			if unit.Service.ExecMainStatus != 0:
				status = RunStatus.ERROR

			unit.Unit.Stop(b'fail')

		return CompletedRun(status, cpu_time, mem_peak, stderr_bytes)

	def get_log(self, ident=None, since=None):
		"""Get journal entries for the runner unit"""

		return self.journal.read(unit=self.unit_name, ident=ident, since=since)
