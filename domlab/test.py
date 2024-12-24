#
# Simple test program using lighttpd
#

import os
from subprocess import run
from tempfile import NamedTemporaryFile

HTTPD_CONF = f'''server.modules += (
	"mod_proxy",
)

server.document-root = "{os.getcwd()}/domlab/static"
server.port = 3000
index-file.names = ("index.htm")

proxy.forwarded = ("for" => 1)
proxy.header = ("upgrade" => "enable")
proxy.server = ("/api/" => (("host" => "{os.getcwd()}/domlab.sock")))
'''

with NamedTemporaryFile(suffix='.conf') as conf:
	conf.write(HTTPD_CONF.encode())
	conf.flush()

	run(('lighttpd', '-D', '-f', conf.name))
	print('done')
