#
# Process the output of the sanitizers
#

import os
import re
from enum import Enum

from ..util import load_builtin_json

UBSAN_ERROR = re.compile(r'([^:]+):(\d+):(\d+): runtime error: (.*)')
ASAN_ERROR = re.compile(r'==\d+==ERROR: AddressSanitizer: SEGV on unknown address')
ASAN_LOCATION = re.compile(r'\s+#\d+ 0x[0-9a-fA-F]+ in ([^ ]+) \(([^+\)]+)')

UBSAN_DIAGS_PATH = 'ubsan-diagnostics.json'


def get_sanitizers_parser(ubsan_diagnostics: dict = None):
	"""Get the SanitizersParser"""

	# Read the built-in UBSan diagnostics
	if ubsan_diagnostics is None:
		ubsan_diagnostics = load_builtin_json(UBSAN_DIAGS_PATH)

		# Compile regular expressions
		for diag in ubsan_diagnostics:
			diag['match'] = re.compile(diag['match'])

	return SanitizersParser(ubsan_diagnostics)


class SanitizersParser:
	"""Parse sanitizer output to identify problems"""

	class _ParseState(Enum):
		NORMAL = 0
		ASAN = 1

	__slots__ = ('ubsan_diagnostics',)

	def __init__(self, ubsan_diagnostics: dict):
		self.ubsan_diagnostics = ubsan_diagnostics

	def parse(self, lines):
		"""Parse all diagnostics from the sanitizers"""

		diags = []  # list of all diagnostics
		current = None  # information for the current diagnostic (ASan)
		state = self._ParseState.NORMAL  # parser state

		for line in lines:
			# Topmost parsing level
			if state == self._ParseState.NORMAL:
				# UndefinedBehaviorSanitizer
				if m := UBSAN_ERROR.match(line):
					message = m.group(4)

					for known_issue in self.ubsan_diagnostics:
						if known_issue['match'].match(message):
							diags.append(known_issue['info'] | {
								'file': os.path.basename(m.group(1)),
								'line': int(m.group(2)),
								'column': int(m.group(3)),
								'raw_message': m.group(4),
							})

				# AddressSanitizer
				if ASAN_ERROR.match(line):
					current = {
						'short': 'intento de acceso a memoria fuera de rango',
						'explains': ['RTE', 'WA'],
						'severity': 7,
						'id': 'out-of-bounds',
						'stack': []
					}

					state = self._ParseState.ASAN

			# Additional information for an ASan diagnostic
			elif state == self._ParseState.ASAN:
				# Write access (instead of read access)
				if 'caused by a WRITE memory access' in line:
					current['short'] = 'intento de escritura en memoria fuera de rango'

				# End of additional information
				elif line.startswith('AddressSanitizer can not provide'):
					state = self._ParseState.NORMAL
					diags.append(current)
					current = None

				elif m := ASAN_LOCATION.match(line):
					current['stack'].append(dict(function=m.group(1), location=m.group(2)))

		return diags
