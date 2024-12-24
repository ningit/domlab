#
# Live web interface to facilitate following student submissions
#

import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .config import load_config
from .webui import DOMlab

logging.basicConfig()
logger = logging.getLogger('domlab')


def decode_listening_address(url: str):
	"""Decode the listening address"""

	# address:port pair
	if ':' in url:
		url_info = urlsplit(f'//{url}')

		try:
			address, port = url_info.hostname, url_info.port

		except ValueError:
			logger.fatal(f'bad listening address: {url}')
			return None

		return address, (port if port else 8888)

	# Assume it is a UNIX socket path
	return url


def main():
	import argparse

	parser = argparse.ArgumentParser(description='Interfaz web para el juez DOMJudge')

	parser.add_argument('-l', '--listen', help='listen in the given address/socket', default='domlab.sock')
	parser.add_argument('-c', '--config', help='configuration file', type=Path, default=Path('config.toml'))
	parser.add_argument('-v', '--verbose', help='increase output verbosity', action='count', default=0)

	args = parser.parse_args()

	# Adjust verbosity
	if args.verbose == 1:
		logger.setLevel(logging.INFO)

	elif args.verbose >= 2:
		logger.setLevel(logging.DEBUG)

	# Parse configuration
	if not args.config.exists():
		logging.error(f'Error: cannot find configuration file {args.config}.')
		return 1

	if (config := load_config(args.config)) is None:
		return 1

	if (address := decode_listening_address(args.listen)) is None:
		return 2

	# Initialize the automatic judge
	app = DOMlab(config)

	try:
		return asyncio.run(app.main(address))

	except KeyboardInterrupt:
		logger.info('shutting down by user request (Ctrl+C)')


sys.exit(main())
