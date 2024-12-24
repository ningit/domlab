#
# Utilities for all components for the package
#

import json
import subprocess
from datetime import datetime
from importlib.resources import files

# Path for builtin known diagnostics
BUILTIN_JSON_PATH = 'data'


def load_builtin_json(name: str):
	"""Load builtin JSON data"""

	with (files() / BUILTIN_JSON_PATH / name).open() as source:
		return json.load(source)


class SyslogReader:
	"""Read information from the system log"""

	__slots__ = ('journal', 'read_method')

	def __init__(self):
		self.journal = None
		try:
			import systemd.journal
			self.journal = systemd.journal
			self.read_method = self._get_with_library

		except ImportError:
			self.read_method = self._get_with_command

	def _get_with_command(self, unit: str, since: datetime, ident: str):
		# Invoke journalctl in the user session with minimal output
		command = ['journalctl', '--user', '-o', 'cat']

		if unit is not None:
			command += ('-u', unit)

		if since is not None:
			command += ('-S', since.strftime('%Y-%m-%d %X'))

		if ident is not None:
			command += ('-t', ident)

		ret = subprocess.run(command, stdout=subprocess.PIPE)

		return [line.decode() for line in ret.stdout.split(b'\n')]

	def _get_with_library(self, unit: str, since: datetime, ident: str):

		reader = self.journal.Reader(self.journal.LOCAL_ONLY | self.journal.CURRENT_USER)
		reader.this_boot()

		if unit is not None:
			reader.add_match(_SYSTEMD_USER_UNIT=unit)

		if since is not None:
			reader.seek_realtime(since.timestamp())

		if ident is not None:
			reader.add_match(SYSLOG_IDENTIFIER=ident)

		messages = []

		# Read all messages under the given constraints
		while event := reader.get_next():
			messages.append(event['MESSAGE'])

		return messages

	def read(self, unit: str = None, since: datetime = None, ident: str = None):
		"""Read events from the system log"""

		return self.read_method(unit, since, ident)
