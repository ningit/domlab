#
# Common definitions for all runners
#

from enum import Enum


class RunStatus(Enum):
	"""Base class for all runners"""

	OK = 0
	TIMEOUT = 1
	OOM = 2      # out of memory
	ERROR = 3    # other error


class CompletedRun:
	"""Information about a complete run"""

	__slots__ = ('status', 'time', 'memory', 'stderr')

	def __init__(self, status, time, memory, stderr=None):
		self.status = status
		self.time = time
		self.memory = memory
		self.stderr = stderr

	def __repr__(self):
		return f'CompletedRun(status={self.status}, time={self.time}, memory={self.memory})'
