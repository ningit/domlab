#
# Deal with Clang-Tidy diagnostics
#

import os
import shutil

import yaml

from ..util import load_builtin_json

# Internal known-diagnostics list
KNOWN_DIAGS_PATH = 'clang-tidy.json'


def get_clang_tidy(known_diags: dict = None):
	"""Get a ClangTidy object"""

	if path := shutil.which('clang-tidy'):
		# Load the built-in list of known diagnostic
		if known_diags is None:
			known_diags = load_builtin_json(KNOWN_DIAGS_PATH)

		return ClangTidy(path, known_diags)


class ClangTidy:
	"""Class to deal with Clang-Tidy"""

	__slots__ = ('clang_tidy', 'known_diags', 'check_filter')

	def __init__(self, path: str, known_diags: dict):
		self.clang_tidy = path
		self.known_diags = known_diags
		self.check_filter = ','.join(known_diags.keys())

	def get_cmdline(self, cxx_files, compiler_args, output_path):
		"""Get the command to run clang-tidy on the given files"""

		return (
			self.clang_tidy, *cxx_files,
			f'--checks={self.check_filter}',  # enable only the checks we can deal with
			'-header-filter=.*',  # also include diagnostics for headers
			f'--export-fixes={output_path}',  # output to the given path
			'--', *compiler_args,  # compiler options
		)

	def explain(self, tidy_yaml_path: str, include_code: bool = True):
		"""Explain errors from a clang-tidy YAML"""

		# clang-tidy does not write the file if there are no diagnostics
		if not os.path.exists(tidy_yaml_path):
			return []

		# Read the YAML file (--export-fixes)
		with open(tidy_yaml_path, 'rb') as tidy:
			tidy_data = yaml.safe_load(tidy)

		messages = []

		# Explain every diagnostic
		for diag in tidy_data.get('Diagnostics', ()):
			diag_name = diag['DiagnosticName']

			# This should not happen because of the filter
			if (info := self.known_diags.get(diag_name)) is None:
				continue

			# Add the information from the Clang-Tidy diagnostic
			message_dict = diag['DiagnosticMessage']

			messages.append(info | {
				'id': diag_name if 'id' not in info else info['id'],
				'file': message_dict['FilePath'],
			 	'offset': message_dict['FileOffset'],
				'raw_message': message_dict['Message'],
			})

		return self._convert_offsets(messages, include_code)

	def _convert_offsets(self, messages, include_code):
		"""Convert file offsets to line and column"""

		# clang-tidy writes byte offsets instead of line and columns in the
		# YAML output, so we have to calculate those by hand (some issues
		# in the LLVM project repository ask for their addition)

		class FileInfo:
			"""Structure that holds the information about a file"""

			__slots__ = ('offset', 'file', 'line_number', 'line')

			def __init__(self, file):
				self.offset = 0  # offset from the beginning of the file
				self.file = open(file, 'rb')  # file object (also line iterator)
				self.line_number = 0  # line count
				self.line = next(self.file)  # current line (or None)

			def __del__(self):
				self.file.close()

			def convert(self, offset):
				# Until offset in the current line
				while offset >= self.offset + len(self.line):
					self.offset += len(self.line)
					self.line = next(self.file)
					self.line_number += 1

				return self.line_number, offset - self.offset, self.line

		files = {}

		# We assume that diagnostics appears in order
		for message in messages:
			offset = message['offset']
			file = message['file']

			# Check whether the file is already known to us
			if (file_info := files.get(file)) is None:
				file_info = FileInfo(file)

			message.pop('offset')

			# Column numbers are not accurate due to multibyte characters.
			# However, we would need to detect the file encoding for
			# calculating the exact character column.
			message['file'] = os.path.basename(file)
			message['line'], message['column'], code = file_info.convert(offset)

			# Include the affected line if requested
			if include_code:
				message['code'] = code.decode()

		return messages
