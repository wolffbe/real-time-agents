from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import json
import os
import uuid
from datetime import datetime, timezone
import pandas as pd
from dotenv import load_dotenv, find_dotenv
import requests as http_requests

# Load .env from project root
load_dotenv(find_dotenv())

AGENT_SERVICE_URL = os.getenv(
    'AGENT_SERVICE_URL',
    'http://agent.real-time-agents.svc.cluster.local'
)

# Optional Hopsworks
fg = None
try:
    import hopsworks
    hopsworks_available = True
except ImportError:
    hopsworks_available = False
    print("Hopsworks not available - continuing without it")

EVENT_TYPES = {
    'page_view': {'category': 'navigation'},
    'button_clicked': {'category': 'interaction'},
    'chat_message_sent': {'category': 'chat'},
    'session_start': {'category': 'session'},
    'session_end': {'category': 'session'},
    'error': {'category': 'error'},
}

sessions = {}

app = Flask(__name__, static_folder='static')
CORS(app, supports_credentials=True)

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

# -------------------------
# Agent proxy
# -------------------------
@app.route('/agent/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def agent_proxy(path):
    url = f"{AGENT_SERVICE_URL}/{path}"

    try:
        resp = http_requests.request(
            method=request.method,
            url=url,
            params=request.args if request.method == 'GET' else None,
            json=request.json if request.method != 'GET' else None,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )

        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get('Content-Type', 'application/json')
        )
    except http_requests.exceptions.RequestException as e:
        return jsonify({'status': 'error', 'message': str(e)}), 503


@app.route('/agent/chat/stream', methods=['POST'])
def agent_stream_proxy():
    """Proxy streaming responses from agent"""
    url = f"{AGENT_SERVICE_URL}/chat/stream"
    data = request.json  # Capture before generator

    def generate():
        try:
            with http_requests.post(
                url,
                json=data,
                headers={'Content-Type': 'application/json'},
                stream=True,
                timeout=60
            ) as resp:
                for line in resp.iter_lines():
                    if line:
                        yield line.decode('utf-8') + '\n\n'
        except http_requests.exceptions.RequestException as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream')

# -------------------------
# Session management
# -------------------------
@app.route('/session/start', methods=['POST'])
def start_session():
    data = request.json or {}
    session_id = str(uuid.uuid4())

    sessions[session_id] = {
        'session_id': session_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'last_activity': datetime.now(timezone.utc).isoformat(),
        'events_count': 0,
        'pages_viewed': [],
        'actions': {},  # action_id -> command
        'last_agent_action': None
    }

    return jsonify({'status': 'success', 'session_id': session_id})

@app.route('/session/end', methods=['POST'])
def end_session():
    session_id = request.json.get('session_id')

    if session_id not in sessions:
        return jsonify({'status': 'error'}), 404

    del sessions[session_id]
    return jsonify({'status': 'success'})

# -------------------------
# Event ingestion
# -------------------------
@app.route('/events', methods=['POST'])
def events():
    data = request.json
    session_id = request.headers.get('X-Session-ID')

    if session_id in sessions:
        sessions[session_id]['events_count'] += 1
        sessions[session_id]['last_activity'] = datetime.now(timezone.utc).isoformat()

        if data.get('event') == 'page_view':
            page = data.get('properties', {}).get('page')
            if page:
                sessions[session_id]['pages_viewed'].append(page)

    return jsonify({'status': 'success'})

# -------------------------
# AGENT → BACKEND (create command)
# -------------------------
@app.route('/webhook/agent-action', methods=['POST'])
def agent_action_webhook():
    data = request.json or {}
    session_id = data.get('session_id')

    if not session_id or session_id not in sessions:
        return jsonify({'status': 'error', 'message': 'Invalid session'}), 400

    action_id = str(uuid.uuid4())

    action = {
        'action_id': action_id,
        'type': data.get('action'),
        'payload': data.get('payload', {}),
        'status': 'pending',
        'created_at': datetime.now(timezone.utc).isoformat()
    }

    sessions[session_id]['actions'][action_id] = action
    sessions[session_id]['last_agent_action'] = datetime.now(timezone.utc).isoformat()

    return jsonify({
        'status': 'received',
        'action_id': action_id
    })

# -------------------------
# FRONTEND ← BACKEND (poll pending commands)
# -------------------------
@app.route('/webhook/pending-actions', methods=['GET'])
def get_pending_actions():
    session_id = request.headers.get('X-Session-ID') or request.args.get('session_id')

    if not session_id or session_id not in sessions:
        return jsonify({'status': 'success', 'actions': []})

    actions = sessions[session_id]['actions']

    pending = [
        action for action in actions.values()
        if action['status'] == 'pending'
    ]

    return jsonify({'status': 'success', 'actions': pending})

# -------------------------
# FRONTEND → BACKEND (ack execution)
# -------------------------
@app.route('/webhook/action-ack', methods=['POST'])
def action_ack():
    data = request.json or {}
    session_id = data.get('session_id')
    action_id = data.get('action_id')
    status = data.get('status', 'executed')

    if session_id not in sessions:
        return jsonify({'status': 'error', 'message': 'Invalid session'}), 400

    actions = sessions[session_id]['actions']

    if action_id not in actions:
        return jsonify({'status': 'error', 'message': 'Unknown action'}), 404

    actions[action_id]['status'] = status
    actions[action_id]['executed_at'] = datetime.now(timezone.utc).isoformat()

    return jsonify({'status': 'ok'})

# -------------------------
# Run
# -------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
