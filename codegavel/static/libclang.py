#
# Custom checks using libclang
#

import os

import clang.cindex as cindex

from ..util import load_builtin_json

# Make SourceLocation hashable
cindex.SourceLocation.__hash__ = lambda self: hash((self.file.name, self.line, self.column))

# Internal path for the compiler diagnostics we want to consider here
KNOWN_DIAGS_PATH = 'clang-diagnostics.json'
CUSTOM_DIAGS_PATH = 'custom-diagnostics.json'


class CallGraphVisitor:
	"""Visitor to compute the call graph of a translation unit"""

	__slots__ = ('parent', 'current_function', 'call_graph', 'main',
	             'functions', 'recursive', 'reachable')

	def __init__(self, parent):
		self.parent = parent

		self.current_function = []
		self.call_graph = {}  # from location to list of locations (to support overloading)
		self.functions = {}  # map from location to name

		self.main = None

		self.recursive = None
		self.reachable = None

	def FUNCTION_DECL(self, node):
		# Skip forward declarations
		if node.get_definition() is not None and node != node.get_definition():
			return False

		self.current_function.append(node.location)
		self.functions[node.location] = node.spelling

		# Locate the main function
		if node.spelling == 'main':
			self.main = node.location

		return True

	def _FUNCTION_DECL(self):
		self.current_function.pop()

	def CXX_METHOD(self, node):
		return self.FUNCTION_DECL(node)

	def _CXX_METHOD(self):
		self._FUNCTION_DECL()

	def CALL_EXPR(self, node):
		if self.current_function:
			if (definition := node.get_definition()) and self.parent._relevant(definition.location):
				self.call_graph.setdefault(self.current_function[-1], set()).add(definition.location)

		return True

	def find_recursive(self):
		"""Find recursive functions in the call graph"""

		self.recursive = set()
		self.reachable = set()

		# Find recursive functions
		pending = set(self.functions.keys()) - {self.main}

		stack = [(self.main, iter(self.call_graph.get(self.main, ())))]
		stack_index = {self.main: 0}

		# Depth-first search
		while stack:
			func, child_it = stack.pop()

			# If there is more children
			if child := next(child_it, None):
				stack.append((func, child_it))

				if child in pending:
					pending.remove(child)

					# Let explore the called function
					stack_index[child] = len(stack)
					stack.append((child, iter(self.call_graph.get(child, ()))))
				else:
					# If there is a cycle
					if (index := stack_index.get(child)) is not None:
						for func, _ in stack[index:]:
							self.recursive.add(func)
			else:
				stack_index.pop(func)

		# Find reachable by recursive function
		pending = set(self.functions.keys()) - {self.main}

		stack = [(self.main, iter(self.call_graph.get(self.main, ())))]

		if self.main in self.recursive:
			self.reachable.add(self.main)

		# Depth-first search (very similar to the above)
		while stack:
			func, child_it = stack.pop()

			# If there is more children
			if child := next(child_it, None):
				stack.append((func, child_it))

				# Mark child as reachable by recursive function
				if func in self.reachable or child in self.recursive:
					self.reachable.add(child)

				if child in pending:
					pending.remove(child)

					# Let explore the called function
					stack.append((child, iter(self.call_graph.get(child, ()))))

		# Warn about unreachable functions by main
		if info := self.parent.custom_diagnostics.get('unreachable-func'):
			for func in pending:
				self.parent.issue(func, 'unreachable-func', info, fname=self.functions[func])


class CommonBugsVisitor:
	"""Visitor to find common bugs in the source code"""

	__slots__ = ('parent', 'func_info')

	def __init__(self, parent, func_info):
		self.parent = parent
		self.func_info = func_info

	def FUNCTION_DECL(self, node):
		if node != node.get_definition():
			return False

		# Check error in argument definitions
		for arg in node.get_arguments():
			# Type with non-trivial constructor passed by value
			if arg.type.kind != cindex.TypeKind.LVALUEREFERENCE and not arg.type.is_pod():
				variant = 'nonpod-by-value-rec' if node.location in self.func_info.reachable else 'nonpod-by-value'

				if info := self.parent.custom_diagnostics.get(variant):
					self.parent.issue(node.location, variant, info, arg_type=arg.type.spelling)

		return True  # because of nested class definitions


def get_cindex_analyzer(known_diagnostics: dict = None, compiler_args: tuple[str, ...] = ()):
	"""Get custom libclang's analyzer"""

	if known_diagnostics is None:
		known_diagnostics = load_builtin_json(KNOWN_DIAGS_PATH)

	custom_diagnostics = load_builtin_json(CUSTOM_DIAGS_PATH)

	return CIndexAnalyzer(known_diagnostics, custom_diagnostics, compiler_args)


class CIndexAnalyzer:
	"""Custom code analyzer using libclang's API"""

	__slots__ = ('compiler_args', 'known_diagnostics', 'custom_diagnostics',
	             'index', 'user_path', 'issues')

	def __init__(self, known_diagnostics: dict, custom_diagnostics: dict, compiler_args=()):
		# Compiler argument for libclang
		self.compiler_args = (*compiler_args, f'-I{self.get_include_path()}')
		# Known diagnostics
		self.known_diagnostics = known_diagnostics
		self.custom_diagnostics = custom_diagnostics
		self.index = None
		self.user_path = None

		# For the AST exploration logic
		self.issues = []

	@staticmethod
	def get_include_path():
		# Obtain the include path by calling clang
		import subprocess

		result = subprocess.run(('clang', '-E', '-v', '-'), input=b'', stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

		if result.returncode != 0:
			raise ValueError('cannot run Clang to obtain the required include path')

		lines = result.stdout.split(b'\n')

		for k in range(len(lines) - 1):
			if lines[k].startswith(b'#include <...> search starts here'):
				return lines[k + 1].strip().decode()

	def _relevant(self, location: cindex.SourceLocation):
		"""Whether the given location is relevant for us"""

		# This may be too restrictive because error in library
		# code are caused by problems in user code
		return location.file.name.startswith(self.user_path)

	def issue(self, location: cindex.SourceLocation, name: str, info: dict, **kwargs):
		"""Report an issue"""

		issue = {'id': name} | info | {
			'file': os.path.basename(location.file.name),
			'line': location.line,
			'column': location.column,
		}

		if kwargs:
			issue['short'] = info['short'].format(**kwargs)

		self.issues.append(issue)

	def _analyze_diagnostics(self, unit: cindex.TranslationUnit):
		"""Analyze the diagnostics of the translation unit"""

		for diag in unit.diagnostics:
			if info := self.known_diagnostics.get(diag.option):
				# This may be too restrictive because error in library
				# code are caused by problems in user code
				if not self._relevant(diag.location):
					continue

				self.issue(diag.location, diag.option, info | dict(raw_message=diag.spelling))

	def _analyze_ast(self, unit, visitor):
		"""Analyze the given unit by the AST"""

		pending = [node for node in unit.cursor.get_children()
		           if self._relevant(node.location)]

		while pending:
			node = pending.pop()

			# node may be a closing function instead of a node
			if not isinstance(node, cindex.Cursor):
				node()
				continue

			recurse = True  # whether the exploration continues recursively

			# Handler for this kind of node
			if handler := getattr(visitor, node.kind.name, None):
				recurse = handler(node)

			if recurse:
				# Closing handler for this kind of node
				if handler := getattr(visitor, f'_{node.kind.name}', None):
					pending.append(handler)

				# Append its children to the pending queue
				for child in node.get_children():
					pending.append(child)

	def analyze(self, cxx_files, user_path, visitors=()):
		"""Analyze the given files"""

		# Index (avoids reparsing when there are multiple files)
		self.index = cindex.Index.create()
		self.user_path = str(user_path)
		self.issues = []  # issues detected so far

		for cxx in cxx_files:
			unit = self.index.parse(cxx, args=self.compiler_args)

			self._analyze_diagnostics(unit)

			# Obtain the call graph of the unit
			cgraph = CallGraphVisitor(self)
			self._analyze_ast(unit, cgraph)
			cgraph.find_recursive()

			# Find common bugs
			cbugs = CommonBugsVisitor(self, cgraph)
			self._analyze_ast(unit, cbugs)

			# Other custom visitors
			for visitor in visitors:
				self._analyze_ast(unit, visitor)

		return self.issues
