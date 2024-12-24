#
# Configuration and persistence
#

import logging

from pathlib import Path


def load_config(filename: Path) -> dict | None:
	"""Load test cases from YAML, JSON or TOML specs"""

	extension = filename.suffix

	# The YAML package is only loaded when needed
	if extension in ('.yaml', '.yml'):
		try:
			import yaml
			from yaml.loader import SafeLoader

		except ImportError:
			logging.error(
				'Cannot load cases from YAML file, since the yaml package is not installed.\n'
				'Please convert the YAML to JSON or install it with pip install pyaml.')
			return None

		# The YAML loader is replaced so that entities have its line number
		# associated to print more useful messages. This is not possible with
		# the standard JSON library.

		class SafeLineLoader(SafeLoader):
			def construct_mapping(self, node, deep=False):
				mapping = super(SafeLineLoader, self).construct_mapping(node, deep=deep)
				# Add 1 so line numbering starts at 1
				mapping['__line__'] = node.start_mark.line + 1
				return mapping

		try:
			with open(filename) as caspec:
				return yaml.load(caspec, Loader=SafeLineLoader)

		except yaml.error.YAMLError as ype:
			logging.error(f'Error while parsing test file: {ype}.')

	# TOML format
	if extension == '.toml':
		try:
			import tomllib

		except ImportError:
			logging.error(
				'Cannot load cases from TOML file, '
				'which is only available since Python 3.11.')
			return None

		try:
			with open(filename, 'rb') as caspec:
				return tomllib.load(caspec)

		except tomllib.TOMLDecodeError as tde:
			logging.error(f'Error while parsing test file: {tde}.')

	# JSON format
	else:
		import json

		try:
			with open(filename) as caspec:
				return json.load(caspec)

		except json.JSONDecodeError as jde:
			logging.error(f'Error while parsing test file: {jde}.')

	return None
