from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv, find_dotenv
import os
import requests as http_requests
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

# Load .env from project root
load_dotenv(find_dotenv())

# Try to import Langfuse
try:
    from langfuse import get_client
    langfuse_available = True
except ImportError:
    langfuse_available = False
    print("Langfuse not available - continuing without observability")

# Initialize Langfuse if credentials exist
langfuse_enabled = (
    langfuse_available
    and bool(os.getenv('LANGFUSE_SECRET_KEY') and os.getenv('LANGFUSE_PUBLIC_KEY'))
)

if langfuse_enabled:
    lf_client = get_client()
    LANGFUSE_HOST = os.getenv('LANGFUSE_HOST', 'http://localhost:3000')
    print(f"Langfuse enabled - dashboard at {LANGFUSE_HOST}")
else:
    lf_client = None

app = Flask(__name__)
CORS(app)

# Web server URL for callbacks (optional)
WEB_SERVER_URL = os.getenv(
    'WEB_SERVER_URL', 'http://web-server.real-time-agents.svc.cluster.local'
)

# Initialize Claude LLM
llm = ChatAnthropic(
    model="claude-sonnet-4-20250514",
    api_key=os.getenv('ANTHROPIC_API_KEY'),
    max_tokens=1024
)


# Define tools for frontend actions
@tool
def click_button(button_text: str) -> str:
    """Click a button on the user's webpage. Use this when the user asks you to perform an action like sending an event or clicking a button.

    Args:
        button_text: The text of the button to click (e.g., "Send Test Event")
    """
    return f"Button '{button_text}' will be clicked"


# LLM with tools bound
tools = [click_button]
llm_with_tools = llm.bind_tools(tools)

# Store conversations in memory (replace with Redis in prod)
conversations = {}


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'agent'})


@app.route('/chat', methods=['POST'])
def chat():
    """Main chat endpoint with Langfuse tracking"""
    data = request.json

    session_id = data.get('session_id', 'default')
    message = data.get('message', '')
    customer_id = data.get('customer_id', 1)
    user_events = data.get('user_events', [])

    # Format user events for context
    events_context = "\n".join(
        [
            f"[{e.get('time', '')}] {e.get('event', '')}"
            + (f": {e.get('button', '')}" if e.get('button') else "")
            + (f": {e.get('error', '')}" if e.get('error') else "")
            for e in user_events[-10:]
        ]
    ) if user_events else "No recent activity"

    # Initialize conversation history
    chat_history = conversations.setdefault(session_id, [])

    # System message
    system_message = SystemMessage(
        content=f"""You are a helpful support assistant.
Keep responses concise and helpful.

Current customer ID: {customer_id}

User's recent activity:
{events_context}"""
    )

    messages = [system_message, *chat_history, HumanMessage(content=message)]

    try:
        if langfuse_enabled:
            with lf_client.start_as_current_span(
                name="chat",
                input={"message": message, "session_id": session_id, "customer_id": customer_id}
            ):
                with lf_client.start_as_current_generation(
                    name="llm-response",
                    model="claude-sonnet-4-20250514",
                    input=message
                ) as gen:
                    response = llm.invoke(messages)
                    response_text = response.content
                    gen.update(output=response_text)
        else:
            response = llm.invoke(messages)
            response_text = response.content

        # Update conversation history
        chat_history.extend([HumanMessage(content=message), AIMessage(content=response_text)])
        conversations[session_id] = chat_history[-20:]

        if langfuse_enabled:
            lf_client.flush()

        return jsonify({'status': 'success', 'response': response_text, 'session_id': session_id})

    except Exception as e:
        print(f"Error in chat: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/chat/stream', methods=['POST'])
def chat_stream():
    """Streaming chat endpoint using Server-Sent Events with tool support"""
    from flask import Response
    import json

    data = request.json

    session_id = data.get('session_id', 'default')
    message = data.get('message', '')
    customer_id = data.get('customer_id', 1)
    user_events = data.get('user_events', [])
    webhook_url = data.get('webhook_url', '')

    # Format user events for context
    events_context = "\n".join(
        [
            f"[{e.get('time', '')}] {e.get('event', '')}"
            + (f": {e.get('button', '')}" if e.get('button') else "")
            + (f": {e.get('error', '')}" if e.get('error') else "")
            for e in user_events[-10:]
        ]
    ) if user_events else "No recent activity"

    # Initialize conversation history
    chat_history = conversations.setdefault(session_id, [])

    # System message
    system_message = SystemMessage(
        content=f"""You are a helpful support assistant with the ability to perform actions on the user's webpage.
Keep responses concise and helpful.

You have access to a click_button tool. When the user asks you to send a test event, click a button, or perform any UI action, use the click_button tool with the appropriate button text.

Available buttons on the page:
- "Send Test Event" - sends a test event

Current customer ID: {customer_id}

User's recent activity:
{events_context}"""
    )

    messages = [system_message, *chat_history, HumanMessage(content=message)]

    def generate():
        full_response = ""
        tool_calls_made = []

        try:
            # First call with tools
            response = llm_with_tools.invoke(messages)

            # Check for tool calls
            if response.tool_calls:
                for tool_call in response.tool_calls:
                    tool_name = tool_call['name']
                    tool_args = tool_call['args']

                    if tool_name == 'click_button':
                        button_text = tool_args.get('button_text', '')
                        tool_calls_made.append(button_text)

                        # Send action to frontend via webhook
                        if webhook_url:
                            try:
                                http_requests.post(
                                    webhook_url,
                                    json={
                                        'session_id': session_id,
                                        'action': 'click_button',
                                        'payload': {'button_text': button_text}
                                    },
                                    timeout=5
                                )
                            except Exception as e:
                                print(f"Webhook error: {e}")

                        # Send action event to frontend
                        yield f"data: {json.dumps({'action': 'click_button', 'button_text': button_text})}\n\n"

                # Generate follow-up response confirming the action
                if tool_calls_made:
                    confirmation = f"Done! I've clicked the \"{tool_calls_made[0]}\" button for you."
                    yield f"data: {json.dumps({'chunk': confirmation})}\n\n"
                    full_response = confirmation

            else:
                # No tool calls - stream the text response
                for chunk in llm_with_tools.stream(messages):
                    if chunk.content:
                        # Handle both string and list content types
                        if isinstance(chunk.content, str):
                            content = chunk.content
                        elif isinstance(chunk.content, list) and len(chunk.content) > 0:
                            # Extract text from content block objects
                            block = chunk.content[0]
                            content = block.get('text', '') if isinstance(block, dict) else str(block)
                        else:
                            content = ""
                        if content:
                            full_response += content
                            yield f"data: {json.dumps({'chunk': content})}\n\n"

            # Update conversation history
            chat_history.extend([HumanMessage(content=message), AIMessage(content=full_response)])
            conversations[session_id] = chat_history[-20:]

            # Log to Langfuse
            if langfuse_enabled:
                with lf_client.start_as_current_span(
                    name="chat_stream",
                    input={"message": message, "session_id": session_id, "customer_id": customer_id}
                ):
                    lf_client.start_generation(
                        name="llm-response",
                        model="claude-sonnet-4-20250514",
                        input=message,
                        output=full_response
                    )
                lf_client.flush()

            # Send done event
            yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"

        except Exception as e:
            print(f"Error in stream: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/chat/reset', methods=['POST'])
def reset_chat():
    """Reset conversation history for a session"""
    data = request.json
    session_id = data.get('session_id', 'default')
    conversations.pop(session_id, None)
    return jsonify({'status': 'success', 'message': 'Conversation reset'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
