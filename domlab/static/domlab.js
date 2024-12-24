//const beep_sound = new Audio('static/beep.mp3');
const IPV4_RGX = /(\d+)\.(\d+)\.(\d+)\.(\d+)/;

function makeCell(msg) {
	const celda = document.createElement('td')
	if (msg) celda.appendChild(document.createTextNode(msg))
	return celda
}

function makeHTMLCell(msg) {
	const celda = document.createElement('td')
	celda.innerHTML = msg
	return celda
}

function makeLinkCell(msg, url) {
	const celda = document.createElement('td')
	const link = document.createElement('a')
	link.setAttribute('href', url)
	link.setAttribute('target', '_blank')
	link.appendChild(document.createTextNode(msg))
	celda.appendChild(link)
	return celda
}

function makeTeamCell(team) {
	if (!team)
		return document.createElement('td')

	const celda = makeLinkCell(team.name, `${sessionStorage.domServerURL}/jury/teams/${team.team_id}`)
	celda.setAttribute('onmouseenter', `showPhoto("${team.team_id}")`)
	celda.setAttribute('onmouseleave', 'hidePhoto()')
	return celda
}

function showPhoto(teamid) {
	const photo = document.getElementById('photo')
	photo.src = `photos/${sessionStorage.serverName}/${teamid}.jpg`
	photo.style.display = 'block';
}

function hidePhoto(teamid) {
	const photo = document.getElementById('photo')
	photo.style.display = 'none';
}

function prettyPrintLocation(ip) {
	// Environment specific
	return ip;
}

function formatDate(date) {
	// Format date with the least uneeded information possible

	const now = new Date();

	// We write day, month and event year if different from the current one
	var prefix = '';

	if (now.getYear() != date.getYear())
		prefix = `${date.getDate()}/${date.getMonth() + 1}/${date.getYear()} `;

	else if  (now.getMonth() != date.getMonth())
		prefix = `${date.getDate()}/${date.getMonth() + 1} `;

	else if (now.getDate() != date.getDate())
		prefix = `${date.getDate()} `;

	return `${prefix} ${date.getHours()}:${date.getMinutes().toString().padStart(2, 0)}<span class="seconds">:${date.getSeconds().toString().padStart(2, 0)}</span>`
}

function startListening(state) {
	const wsUrl = new URL(location.href + '/api/feed')
	wsUrl.protocol = (location.protocol == 'https:') ? 'wss:' : 'ws:'

	const socket = new WebSocket(wsUrl);

	const since = document.getElementById('startime-select').valueAsNumber;
	const until = document.getElementById('endtime-select').valueAsNumber;

	socket.addEventListener('open', function (event) {
		socket.send(JSON.stringify({
			type: 'subscribe',
			server: state.server,
			subject: state.subject,
			token: '__token__',
			since: since ? since / 1000: null,
			until: until ? until / 1000 : null,
		}));
	});

	socket.addEventListener('message', function (event) {
		const msg = JSON.parse(event.data);
		const table = document.getElementById('mainTable').tBodies[0]

		if (msg.type == 'submission') {
			const fila = document.createElement('tr')
			fila.setAttribute('id', `sub${msg.sid}`)

			// Número de envío
			fila.appendChild(makeLinkCell(msg.sid,
				`${sessionStorage.domServerURL}/jury/submissions/${msg.sid}`))

			// Primero de los autores
			fila.appendChild(makeTeamCell(msg.submitter))

			// Segundo de los autores
			fila.appendChild(makeTeamCell(msg.other[0]))

			// Otras informaciones
			const where = prettyPrintLocation(msg.ip);
			fila.appendChild(makeCell(where.lab))
			fila.appendChild(makeCell(where.post))


			fila.appendChild(makeCell(msg.problem))

			// Submission time (can be reduced)
			const date = new Date(msg.time);
			fila.appendChild(makeHTMLCell(formatDate(date)))


			// Veredicto
			celda = document.createElement('td')
			if (msg.judgement?.verdict) {
				celda.appendChild(document.createTextNode(msg.judgement.verdict))
				celda.setAttribute('class', msg.judgement.verdict)
			}
			fila.appendChild(celda)

			// Análisis
			celda = document.createElement('td')
			const button = document.createElement('button');
			button.innerText = '🔍';
			button.addEventListener('click', () => fetchAnalysis(msg.sid, msg.time));
			celda.appendChild(button);
			fila.appendChild(celda);

			table.insertBefore(fila, table.firstChild)
			// beep_sound.play();
		}
		else if (msg.type == 'update') {
			const fila = document.getElementById(`sub${msg.sid}`)
			fila.lastChild.previousElementSibling.textContent = msg.verdict
			fila.lastChild.previousElementSibling.setAttribute('class', msg.verdict)
		}
		else if (msg.type == 'error') {
			alert(`Error: ${msg.reason}`)
		}
	});

	document.socket = socket;
}

function closeModal(modalid) {
	document.getElementById(modalid).style.display = 'none';
}

function subjectClick(click) {
	// Date range is read from the date input fields
	const since = document.getElementById('startime-select').valueAsNumber;
	const until = document.getElementById('endtime-select').valueAsNumber;

	// Collect the subject data to start listening
	const state = {
		'mode': 'subject',
		server: click.currentTarget.dataset.server,
		subject: click.currentTarget.dataset.subject,
		url: click.currentTarget.dataset.url,
		since: since ? since / 1000 : null,
		until: until ? until / 1000 : null,
	};

	startSubject(state);
}

async function fetchAnalysis(sid, time) {
	const appState = JSON.parse(sessionStorage.getItem('appState'));

	fetch('api/diagnostic', {
		method: 'POST',
		body: new URLSearchParams({
			server: appState.server,
			subject: appState.subject,
			sid: sid,
			timestamp: time,
		}),
	}).then(response => response.text())
	.then(text => showMessage(text));
}

async function loadHome() {
	const home = document.getElementById('homeFrame');
	home.innerHTML = '';

	// Download the list of course
	const response = await fetch('api/home');
	const answer = await response.json();

	for (let course of answer.subjects) {
		// Create a button for each subject
		const block = document.createElement('button');
		block.setAttribute('class', 'course-block')
		block.setAttribute('data-subject', course.subject);
		block.setAttribute('data-server', course.server);
		block.setAttribute('data-url', course.server_url);

		// Print server and course name
		var span = document.createElement('span');
		span.innerText = `${course.server}/`;
		block.appendChild(span);

		span = document.createElement('span');
		span.innerText = course.subject;
		block.appendChild(span);
		home.appendChild(block);

		// Add event listener
		block.addEventListener('click', subjectClick);
	}

	// Set the date field to today
	var today = new Date();
	today.setUTCMilliseconds(0);
	today.setUTCSeconds(0);
	today.setUTCMinutes(0);
	today.setUTCHours(0);
	const datesince = document.getElementById('startime-select');
	datesince.valueAsNumber = today.getTime();
}

async function startApp() {
	const appStateStr = sessionStorage.getItem('appState');

	// We load home when the app state is empty
	if (appStateStr == null)
		loadHome();
	else {
		const appState = JSON.parse(appStateStr);

		if (appState.mode == 'subject') {
			startSubject(appState);
		}
	}
}

function showMessage(message) {
	const modal = document.getElementById('message-modal');
	const content = document.getElementById('message-modal-body');

	content.innerHTML = message;
	modal.style.display = 'flex';
}

function serverLogin(server, callback) {
	const modal = document.getElementById('login-modal');
	const serverCell = document.getElementById('lm-server');
	const submit = document.getElementById('lm-submit');

	serverCell.innerText = server.replace('https://', '').replace(/(\/domjudge)$/, '')
	modal.style.display = 'flex';
	submit.onclick = function () {
		modal.style.display = 'none';
		callback();
	}
}

function startSubject(state) {
	document.getElementById('headerTitle').innerText = `${state.server}/${state.subject}`;
	document.getElementById('homeFrame').style.display = 'none';
	document.getElementById('mainFrame').style.display = 'initial';
	document.getElementById('home-options').style.display = 'none';
	document.getElementById('filter-options').style.display = 'initial';
	document.getElementById('close-button').style.display = 'initial';

	sessionStorage.domServerURL = state.url;
	sessionStorage.serverName = state.server;
	sessionStorage.appState = JSON.stringify(state);

	startListening(state);
}

function closeSubject() {
	sessionStorage.clear();
	location.reload();

	// Instead of reloading, we can change screens and close the websocket
}
