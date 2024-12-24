#
# Generate an HTML representation of the summary
#

def make_summary(summary: dict):
	"""Build an HTML representation of the summary"""

	if not summary:
		return 'No hay información adicional.'

	blocks = ['<ul>']

	# Avoid including extra explanations twice
	extra_seen = set()

	for diagnostic in summary:
		# Location for the diagnostic
		location = diagnostic['file']

		if (line := diagnostic.get('line')) is not None:
			location += f':{line}'
			if (column := diagnostic.get('column')) is not None:
				location += f':{column}'

		blocks.append(f'<li><b>{location}:</b> {diagnostic["short"]}')

		if extra := diagnostic.get('extra'):
			if (diag_id := diagnostic.get('id')) not in extra_seen:
				blocks.append(f'<p><i>{extra}</i>')
				extra_seen.add(diag_id)
		elif raw_message := diagnostic.get('raw_message'):
			blocks.append(f'<p><i>{raw_message}</i>')

	blocks.append('</ul>')

	return '\n'.join(blocks)
