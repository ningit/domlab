#
# Code evaluation with static and runtime methods
#

import json
import os
import shutil
import subprocess
from datetime import datetime
from enum import Enum
from pathlib import Path

from .runner.common import RunStatus
from .runtime.sanitizers import get_sanitizers_parser


class Toolchain:
	"""Compiler and execution toolchain"""

	__slots__ = ('compiler_args', 'cxx', 'clang_tidy', 'libclang',
	             'runner', 'sanitizers_parser')

	def __init__(self, compiler_args=(), sanitizers_diags: dict = None):
		self.compiler_args = compiler_args

		self.cxx = None
		self.clang_tidy = None
		self.libclang = None
		self.runner = None
		self.sanitizers_parser = get_sanitizers_parser(sanitizers_diags)

		self._scan()

	def _scan(self):
		"""Check the available compilers and tools"""

		self._scan_compiler()
		self._scan_clang_tidy()
		self._scan_clang_cindex()
		self._scan_systemd()

	def _scan_compiler(self):
		"""Check for the preferred compiler"""

		# Check the CXX environment variable
		self.cxx = os.getenv('CXX')

		if self.cxx is None:
			for name in ('c++', 'g++', 'clang++'):
				self.cxx = shutil.which(name)
				if self.cxx is not None:
					break

		elif shutil.which(self.cxx) is None:
			print('Error: wrong compiler specified in the CXX variable.')
			self.cxx = None

	def _scan_clang_tidy(self):
		"""Check for clang-tidy for static analysis"""

		from .static.clangtidy import get_clang_tidy
		self.clang_tidy = get_clang_tidy()

	def _scan_clang_cindex(self):
		"""Check for clang.cindex API"""

		try:
			from .static.libclang import get_cindex_analyzer
			self.libclang = get_cindex_analyzer(compiler_args=self.compiler_args)

		except ImportError:
			pass

	def _scan_systemd(self):
		"""Check for systemd"""

		try:
			from .runner.systemd import SystemdRunner
			self.runner = SystemdRunner()

		except ImportError:
			pass

	@staticmethod
	def _get_version(command):
		"""Get the version from a command"""

		result = subprocess.run((command, '--version'), stdout=subprocess.PIPE)
		return result.stdout.split(b'\n', maxsplit=1)[0].decode('utf-8')

	def dump_info(self):
		"""Show information about the available tools"""

		return (
			('Compiler', self._get_version(self.cxx) if self.cxx else 'no'),
			('clang-tidy', 'yes' if self.clang_tidy else 'no'),
			('libclang', f'yes ({self._get_version("clang")})' if self.libclang else 'no'),
			('systemd', f'yes ({self._get_version("systemctl")})' if self.runner else 'no'),
		)

	@property
	def has_systemd(self):
		"""Whether systemd is available"""

		return self.runner is not None

	@property
	def compiler_command(self):
		"""Command to run the compiler"""

		return self.cxx, *self.compiler_args

	def new_submission(self, *args, **kwargs):
		"""Create a new submission with this toolchain"""

		return Submission(self, *args, **kwargs)

	def build(self, cxx_files, compiler_args, binary_path: Path, compiler_out, *,
	          timeout=30, memlimit=2000, unit_name=None):
		"""Build a binary"""

		# Compiler command-line arguments
		binary_path = binary_path.absolute()

		cmdline = (*self.compiler_command, *compiler_args, *map(os.path.abspath, cxx_files), '-o', binary_path)

		if self.runner is None:
			try:
				result = subprocess.run(cmdline, stdout=compiler_out,
				                        stderr=subprocess.STDOUT, timeout=timeout).returncode == 0

			except subprocess.TimeoutExpired:
				result = False
		else:
			result = self.runner(cmdline, stdout=compiler_out, stderr=compiler_out, timeout=timeout,
			                     memlimit=memlimit, write_dirs=(binary_path.parent,),
			                     unit_name=unit_name).status == RunStatus.OK

		return result


class Verdict(Enum):
	"""Verdict of a submission"""

	AC = 0  # accepted
	WA = 1  # wrong answer
	RTE = 2  # runtime error
	TLE = 3  # time limit error
	MLE = 4  # memory limit error
	OLE = 5  # output limit error


class VerdictMetadata:
	"""Verdict of a submission output check"""

	def __init__(self, verdict, time, memory, diagnostics=None):
		self.verdict = verdict
		self.time = time
		self.memory = memory
		self.diagnostics = diagnostics

	def __repr__(self):
		return f'VerdictMetadata(verdict={self.verdict}, time={self.time}, memory={self.memory})'


class Submission:
	"""Code submission to be analyzed"""

	STATIC_DIAGS_PATH = 'static-analysis.json'
	CUSTOM_DIAGS_PATH = 'custom-analysis.json'
	SANITIZER_DIAGS_PATH = '{case}-sanitizers.json'
	PROGRAM_NAME = 'program'
	COMPILER_OUTPUT = 'compiler.txt'

	def __init__(self, toolchain, source_dir, include_dirs=(), compiler_args=(), work_dir=None, output_dir=None):
		self.toolchain = toolchain
		self.source_dir = Path(source_dir)
		self.include_dirs = include_dirs
		self.compiler_args = compiler_args

		# Submission directory using current time
		subm_dir = Path(datetime.now().isoformat().replace(':', ''))

		# If work_dir or output_dir is None, figure out a local directory
		if work_dir is None:
			self.work_dir = subm_dir / 'work'
			self.work_dir.mkdir()
		else:
			self.work_dir = Path(work_dir)

		if output_dir is None:
			self.output_dir = subm_dir / 'output'
			self.output_dir.mkdir()
		else:
			self.output_dir = Path(output_dir)

		# Ensure these directories exist
		self.work_dir.mkdir(exist_ok=True)
		self.output_dir.mkdir(exist_ok=True)

		# Collect source files
		self._collect_files()

		# Compiled binary (with or without instrumentation)
		self.binaries = {}

	def _collect_files(self):
		"""Collect source files"""

		self.cxx_files, self.header_files, self.unknown_files = [], [], []

		for path in self.source_dir.iterdir():
			match path.suffix.lower():
				# C++ source files
				case '.cpp' | '.cc' | '.cxx' | '.c++' | '.c':
					self.cxx_files.append(path)
				# C++ header files
				case '.h' | '.hpp' | '.hh' | '.hxx':
					self.header_files.append(path)
				case _:
					self.unknown_files.append(path)

	def build(self, instrument=False, unit_name=None):
		"""Build the binary for this submission"""

		# Output binary path
		binary_path = self.work_dir / self.PROGRAM_NAME
		compiler_args = self.compiler_args

		# Adaptations when using instrumentation
		if instrument:
			binary_path = binary_path.with_suffix('.instr')
			compiler_args = (*compiler_args, '-fsanitize=address,undefined', '-g')

		with (self.output_dir / self.COMPILER_OUTPUT).open('w') as compiler_out:
			if not self.toolchain.build(self.cxx_files, compiler_args, binary_path,
			                            compiler_out, unit_name=unit_name):
				return False

		self.binaries[instrument] = binary_path.absolute()
		return True

	def check_static(self):
		"""Check the program with static analyzers"""

		# Only if available
		if not self.toolchain.clang_tidy:
			return None

		# Run Clang-Tidy unconfined
		output_path = self.output_dir / "clang-tidy.yaml"

		cmdline = self.toolchain.clang_tidy.get_cmdline(
			self.cxx_files,
			(*self.toolchain.compiler_args, *self.compiler_args),
			output_path
		)

		subprocess.run(cmdline, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

		# Explain the diagnostics for the user
		diagnostics = self.toolchain.clang_tidy.explain(output_path)

		with (self.output_dir / self.STATIC_DIAGS_PATH).open('w') as out:
			json.dump(diagnostics, out)

		return diagnostics

	def check_custom(self):
		"""Check the program with custom static analysis"""

		# Only if available
		if not self.toolchain.libclang:
			return None

		diagnostics = self.toolchain.libclang.analyze(self.cxx_files, user_path=self.source_dir)

		with (self.output_dir / self.CUSTOM_DIAGS_PATH).open('w') as out:
			json.dump(diagnostics, out)

		return diagnostics

	def check_output(self, input, expected, output, comparator=None, instrument=False,
	                 timelimit=None, memlimit=None, unit_name=None):
		"""Check the program output for a given test case"""

		# Get the binary or build it if not done yet
		if (binary := self.binaries.get(instrument)) is None:
			if not self.build(instrument=instrument, unit_name=unit_name):
				return None
			binary = self.binaries.get(instrument)

		# When the run was started
		before_time = datetime.now()

		# When running with instrumentation, we tell UBSan and ASan to dump diagnostics also
		# to the system log using the 'log_to_syslog' option. There is also an option 'log_path'
		# so that diagnostics are dumped to a file instead of standard error, but we want them
		# to appear also in standard error for manual inspection.
		env = dict(ASAN_OPTIONS='log_to_syslog=1', UBSAN_OPTIONS='log_to_syslog=1') if instrument else None

		with open(input) as inputf, open(output, 'w') as outputf:
			result = self.toolchain.runner((str(binary),), stdin=inputf, stdout=outputf, stderr=outputf,
			                               timeout=timelimit, memlimit=memlimit, task_limit=2, env=env,
			                               unit_name=unit_name)

		match result.status:
			case RunStatus.OK:
				# Compare the actual output with the expected one
				if comparator is None:
					same = output.read_bytes() == expected.read_bytes()
				else:
					same = comparator(input, output, expected)

				verdict = Verdict.AC if same else Verdict.WA

			case RunStatus.TIMEOUT:
				verdict = Verdict.TLE

			case RunStatus.OOM:
				verdict = Verdict.MLE

			case RunStatus.ERROR:
				verdict = Verdict.RTE

		# Check instrumentation results
		diagnostics = None

		if instrument:
			sanitizer_log = self.toolchain.runner.get_log(ident=binary.name, since=before_time)

			if diagnostics := self.toolchain.sanitizers_parser.parse(sanitizer_log):
				with (self.output_dir / self.SANITIZER_DIAGS_PATH.format(case=input.stem)).open('w') as sanitizers_dump:
					json.dump({'verdict': verdict.name, 'diagnostics': diagnostics}, sanitizers_dump)

		return VerdictMetadata(verdict, result.time, result.memory, diagnostics=diagnostics)

	def summary(self, min_severity=4, must_explain=True, verdicts=()):
		"""Obtain a summary by all the methods"""

		# Summary of diagnostics
		summary = Summary(min_severity=min_severity)
		verdicts = set(verdicts)

		# Instrumentation test with sanitizers
		for log_path in self.output_dir.glob(self.SANITIZER_DIAGS_PATH.format(case='*')):
			with log_path.open() as log_file:
				log = json.load(log_file)

			verdict = log['verdict']
			verdicts.add(verdict)

			for diag in log['diagnostics']:
				if not must_explain or verdict in diag.get('explains', ()):
					summary.add(diag)

		# Static analysis with Clang-Tidy and custom diagnostics
		static_diags = (
			self.output_dir / self.STATIC_DIAGS_PATH,
			self.output_dir / self.CUSTOM_DIAGS_PATH,
		)

		for diag_source in static_diags:
			if diag_source.exists():
				with diag_source.open() as source:
					log = json.load(source)

				for diag in log:
					if not must_explain or not verdicts.isdisjoint(diag.get('explains', ())):
						summary.add(diag)

		summary.sort(verdicts=verdicts)
		return summary.diagnostics


class Summary:
	"""Summary of diagnostics"""

	def __init__(self, min_severity=4):
		self.min_severity = min_severity
		self.diagnostics = []
		self.seen = set()

	@staticmethod
	def _seen_key(diagnostic):
		"""Seen key to avoid repetitions"""

		return (diagnostic.get('id'), diagnostic.get('file'),
		        diagnostic.get('line'), diagnostic.get('column'),
		        hash(diagnostic.get('short')))

	def add(self, diagnostic):
		"""Add a diagnostic to the summary"""

		key = self._seen_key(diagnostic)

		# Avoid duplicated entries and filter by severity
		if key not in self.seen and diagnostic.get('severity', 0) >= self.min_severity:
			self.diagnostics.append(diagnostic)
			self.seen.add(key)

	def sort(self, verdicts=()):
		"""Sort by severity"""

		# If verdicts are given, we place diagnostics that explain
		# those verdicts before any other diagnostic
		if verdicts:
			class ExplainedOrder:
				def __init__(self):
					self.verdicts = set(verdicts)

				def __call__(self, diag):
					return (
						0 if self.verdicts.isdisjoint(diag.get('explains', ())) else 1,
					 	diag.get('severity', 0)
					)

			order = ExplainedOrder()
		else:
			def order(diag):
				return diag.get('severity', 0)

		self.diagnostics.sort(key=order, reverse=True)
