#
# Show whether the required tools are available and their versions
#

import sys

from . import Toolchain


def main():
	# Dump availability and version of tools
	for name, version in Toolchain().dump_info():
		version = version.replace('yes', '\x1b[1;32myes\x1b[0m')
		print(f'\x1b[1m{name + ":":12}\x1b[0m {version}')

	return 0


sys.exit(main())
