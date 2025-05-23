#!/usr/bin/env python3
"""
DeepSeek-Coder Web UI
A Quart-based application that provides a web interface for interacting with DeepSeek-Coder model
running on Ollama, with PostgreSQL-based authentication and chat persistence.
"""

import os
import json
import uuid
import asyncio
import shortuuid
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple, Union

import asyncpg
import httpx
import markdown
import aiofiles
from slugify import slugify
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from quart import (
    Quart, render_template, request, redirect, url_for, jsonify,
    session, websocket, Response
)
from quart_auth import (
    QuartAuth, AuthUser, current_user, login_required,
    login_user, logout_user, Unauthorized
)
from quart_session import Session
from hypercorn.config import Config
from hypercorn.asyncio import serve

import re
import json
from urllib.parse import urlparse, unquote
from github_connector import GithubConnector, format_code_for_llm, summarize_repository_for_llm


# Monkey patch AuthUser to add user_id property
def user_id_property(self):
    return self.auth_id

AuthUser.user_id = property(user_id_property)

# Environment variables
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # Optional GitHub token for higher rate limits
POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "deepseek")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-coder:33b-instruct-q5_K_M")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "ollama")
OLLAMA_PORT = os.environ.get("OLLAMA_PORT", "11434")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "changeme_in_production")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")


# Available DeepSeek models
AVAILABLE_MODELS = [
    {"id": "deepseek-coder:1.3b-instruct-q4_0", "name": "DeepSeek-Coder 1.3B", "description": "Smallest model, fastest response"},
    {"id": "deepseek-coder:6.7b-instruct-q5_K_M", "name": "DeepSeek-Coder 6.7B", "description": "Balanced model, good performance"},
    {"id": "deepseek-coder:33b-instruct-q5_K_M", "name": "DeepSeek-Coder 33B", "description": "Largest model, best quality"}
]

# Application setup
app = Quart(__name__)
app.config["SECRET_KEY"] = SESSION_SECRET
app.config["QUART_AUTH_COOKIE_SECURE"] = False  # Set to True in production with HTTPS
app.config["QUART_AUTH_COOKIE_DOMAIN"] = None
app.config["QUART_AUTH_COOKIE_NAME"] = "deepseek_auth"
app.config["QUART_AUTH_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_TYPE"] = "null"  # Added to prevent warning
app.config["RESPONSE_TIMEOUT"] = 600  # 10 minutes in seconds
app.config["BODY_TIMEOUT"] = 600  # 10 minutes in seconds

# Initialize Auth
auth_manager = QuartAuth(app)
Session(app)

# Global database connection pool
db_pool = None

class UserID(AuthUser):
    """Custom class for storing user information in the session"""
    def __init__(self, auth_id):
        super().__init__(auth_id)
        self._auth_id = auth_id  # Store as private attribute

    @property
    def auth_id(self):
        return self._auth_id

    @property
    def user_id(self):
        return self._auth_id

@app.before_serving
async def setup_db():
    """Initialize database connection pool before serving"""
    global db_pool
    db_pool = await asyncpg.create_pool(
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        database=POSTGRES_DB,
        host=POSTGRES_HOST,
        port=POSTGRES_PORT
    )
    
    # Check if default admin exists, create if not
    async with db_pool.acquire() as conn:
        admin = await conn.fetchrow(
            "SELECT id FROM users WHERE username = $1",
            ADMIN_USERNAME
        )
        
        if not admin:
            # Create admin user
            await conn.execute(
                """
                INSERT INTO users (username, password, email, is_admin, full_name) 
                VALUES ($1, $2, $3, $4, $5)
                """,
                ADMIN_USERNAME,
                ADMIN_PASSWORD,
                f"{ADMIN_USERNAME}@example.com",
                True,
                "System Administrator"
            )
            print(f"Created default admin user: {ADMIN_USERNAME}")

@app.after_serving
async def close_db():
    """Close database connection pool after serving"""
    if db_pool:
        await db_pool.close()

@app.errorhandler(Unauthorized)
async def unauthorized(error):
    """Handle unauthorized access attempts"""
    return redirect(url_for("login", next=request.path))

@app.route("/login", methods=["GET", "POST"])
async def login():
    """Handle user login"""
    error = None
    
    if await current_user.is_authenticated:
        return redirect(url_for("index"))
    
    if request.method == "POST":
        form = await request.form
        username = form.get("username")
        password = form.get("password")
        
        if not username or not password:
            error = "Username and password are required"
        else:
            # Check credentials against database
            async with db_pool.acquire() as conn:
                user = await conn.fetchrow(
                    "SELECT id, username, password, is_locked, locked_until FROM users WHERE username = $1",
                    username
                )
                
                if user and user["is_locked"] and user["locked_until"] > datetime.now():
                    # Account is locked
                    remaining_time = user["locked_until"] - datetime.now()
                    error = f"Account is locked. Try again in {remaining_time.seconds // 60} minutes."
                elif user and user["password"] == password:
                    # Reset login attempts on successful login
                    await conn.execute(
                        "UPDATE users SET login_attempts = 0, last_login = CURRENT_TIMESTAMP WHERE id = $1",
                        user["id"]
                    )
                    
                    # Create a session for the user
                    user_id_obj = UserID(user["id"])
                    login_user(user_id_obj)
                    
                    # Record the session in the database
                    session_id = str(uuid.uuid4())
                    session["session_id"] = session_id
                    
                    await conn.execute(
                        """
                        INSERT INTO sessions 
                        (session_id, user_id, expires_at, ip_address, user_agent) 
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        session_id,
                        user["id"],
                        datetime.now() + timedelta(days=1),
                        request.remote_addr,
                        request.headers.get("User-Agent", "Unknown")
                    )
                    
                    next_url = request.args.get("next", "/")
                    return redirect(next_url)
                else:
                    # Increment login attempts
                    if user:
                        login_attempts = user["login_attempts"] + 1
                        # Lock account after 5 failed attempts
                        if login_attempts >= 5:
                            await conn.execute(
                                """
                                UPDATE users 
                                SET login_attempts = $1, is_locked = TRUE, locked_until = $2 
                                WHERE id = $3
                                """,
                                login_attempts,
                                datetime.now() + timedelta(minutes=15),
                                user["id"]
                            )
                            error = "Too many failed attempts. Account locked for 15 minutes."
                        else:
                            await conn.execute(
                                "UPDATE users SET login_attempts = $1 WHERE id = $2",
                                login_attempts,
                                user["id"]
                            )
                    
                    error = "Invalid username or password"
    
    return await render_template("login.html", error=error)

@app.route("/logout")
@login_required
async def logout():
    """Handle user logout"""
    # Remove session from database
    if "session_id" in session:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE session_id = $1",
                session["session_id"]
            )
    
    # Clear session
    logout_user()
    return redirect(url_for("login"))

@app.route("/")
@login_required
async def index():
    """Main page - show chat history"""
    # Get user's chat history
    user_id = current_user.user_id
    
    async with db_pool.acquire() as conn:
        # Get user info
        user = await conn.fetchrow(
            "SELECT username, full_name, is_admin FROM users WHERE id = $1",
            user_id
        )
        
        # Get user's chats
        chats = await conn.fetch(
            """
            SELECT id, title, created_at, updated_at 
            FROM chats 
            WHERE user_id = $1 AND NOT is_archived 
            ORDER BY updated_at DESC
            """,
            user_id
        )
        
        # Format dates
        formatted_chats = []
        for chat in chats:
            formatted_chats.append({
                "id": chat["id"],
                "title": chat["title"],
                "created_at": chat["created_at"].strftime("%Y-%m-%d %H:%M"),
                "updated_at": chat["updated_at"].strftime("%Y-%m-%d %H:%M")
            })
    
    return await render_template(
        "index.html", 
        user=user, 
        chats=formatted_chats,
        models=AVAILABLE_MODELS,
        selected_model=session.get("selected_model", MODEL_NAME)
    )

@app.route("/list-models")
@login_required
async def list_models():
    """List available Ollama models"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/tags")
            
            if response.status_code == 200:
                models = response.json().get("models", [])
                # Filter for deepseek models
                deepseek_models = [m for m in models if "deepseek" in m["name"].lower()]
                
                if deepseek_models:
                    return jsonify({"success": True, "models": deepseek_models})
                else:
                    # Return predefined models if no deepseek models found
                    return jsonify({"success": True, "models": AVAILABLE_MODELS})
            else:
                # Fall back to predefined models
                return jsonify({"success": True, "models": AVAILABLE_MODELS})
    except Exception as e:
        print(f"Error fetching models: {e}")
        return jsonify({"success": True, "models": AVAILABLE_MODELS})

@app.websocket("/ws/chat/<int:chat_id>")
@login_required
async def ws_chat(chat_id):
    """WebSocket endpoint for chat"""
    user_id = current_user.user_id
    
    # Verify that the chat belongs to the user
    async with db_pool.acquire() as conn:
        chat = await conn.fetchrow(
            "SELECT id FROM chats WHERE id = $1 AND user_id = $2",
            chat_id, user_id
        )
        
        if not chat:
            return
    
    await websocket.accept()
    
    while True:
        try:
            # Receive message from client
            message_data = await websocket.receive_json()
            content = message_data.get("content", "").strip()
            
            if not content:
                await websocket.send_json({"error": "Empty message"})
                continue
            
            # Get currently selected model
            model = session.get("selected_model", MODEL_NAME)
            
            # Save user message to database
            async with db_pool.acquire() as conn:
                user_msg_id = await conn.fetchval(
                    """
                    INSERT INTO messages (chat_id, role, content) 
                    VALUES ($1, $2, $3) 
                    RETURNING id
                    """,
                    chat_id, "user", content
                )
            
            # Send confirmation of user message
            await websocket.send_json({
                "type": "message",
                "id": user_msg_id,
                "role": "user",
                "content": content,
                "timestamp": datetime.now().strftime("%H:%M")
            })
            
            # Process with Ollama
            try:
                # Fetch previous messages in this chat to maintain context
                async with db_pool.acquire() as conn:
                    previous_messages = await conn.fetch(
                        """
                        SELECT role, content
                        FROM messages
                        WHERE chat_id = $1
                        ORDER BY created_at
                        """,
                        chat_id
                    )
                
                # Build the conversation context
                system_prompt = "You are DeepSeek-Coder, an AI assistant specialized in helping with programming tasks."
                prompt = system_prompt + "\n\n"
                
                # Add conversation history - limit to last 10 exchanges to avoid token limits
                # Skip the most recent message as we'll add it separately
                history_messages = previous_messages[:-1]
                # Take the last 10 exchanges (up to 20 messages)
                if len(history_messages) > 20:
                    history_messages = history_messages[-20:]
                
                for msg in history_messages:
                    role = "User" if msg["role"] == "user" else "Assistant"
                    prompt += f"{role}: {msg['content']}\n\n"
                
                # Add the current user message
                prompt += f"User: {content}\n\nAssistant:"
                
                # Increase the timeout for Ollama API calls
                async with httpx.AsyncClient(timeout=600.0) as client:  # Increased from 120.0
                    response = await client.post(
                        f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/generate",
                        json={
                            "model": model,
                            "prompt": prompt,
                            "stream": True
                        },
                        headers={"Content-Type": "application/json"},
                    )
                    
                    # Start with empty response
                    full_response = ""
                    
                    # Create assistant message in database
                    async with db_pool.acquire() as conn:
                        assistant_msg_id = await conn.fetchval(
                            """
                            INSERT INTO messages (chat_id, role, content) 
                            VALUES ($1, $2, $3) 
                            RETURNING id
                            """,
                            chat_id, "assistant", ""
                        )
                    
                    # Process streaming response
                    buffer = b""
                    async for chunk in response.aiter_bytes():
                        buffer += chunk
                        
                        # Process complete JSON objects
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if line.strip():
                                try:
                                    chunk_data = json.loads(line)
                                    if "response" in chunk_data:
                                        token = chunk_data["response"]
                                        full_response += token
                                        
                                        # Send token to client
                                        await websocket.send_json({
                                            "type": "token",
                                            "token": token,
                                            "id": assistant_msg_id
                                        })
                                        
                                        # If done flag is True, break
                                        if chunk_data.get("done", False):
                                            break
                                except json.JSONDecodeError:
                                    continue
                    
                    # Update the complete message in the database
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE messages SET content = $1 WHERE id = $2",
                            full_response, assistant_msg_id
                        )
                    
                    # Process the response for code blocks and extract artifacts
                    code_blocks = extract_code_blocks(full_response)
                    for idx, code_block in enumerate(code_blocks):
                        language = code_block.get("language", "")
                        code = code_block.get("code", "")
                        
                        # Generate a title based on content
                        if language:
                            title = f"Code Snippet {idx+1} ({language})"
                        else:
                            title = f"Code Snippet {idx+1}"
                        
                        # Save artifact to database
                        async with db_pool.acquire() as conn:
                            artifact_id = await conn.fetchval(
                                """
                                INSERT INTO artifacts 
                                (chat_id, message_id, title, content, content_type, language) 
                                VALUES ($1, $2, $3, $4, $5, $6) 
                                RETURNING id
                                """,
                                chat_id, assistant_msg_id, title,
                                code, "text/plain", language
                            )
                        
                        # Notify client about new artifact
                        await websocket.send_json({
                            "type": "artifact",
                            "id": artifact_id,
                            "title": title,
                            "language": language,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
                        })
                    
                    # Send completion message
                    await websocket.send_json({
                        "type": "complete",
                        "id": assistant_msg_id,
                        "role": "assistant",
                        "content": full_response,
                        "timestamp": datetime.now().strftime("%H:%M")
                    })
                    
            except Exception as e:
                print(f"Error processing with Ollama: {e}")
                await websocket.send_json({
                    "type": "error",
                    "error": str(e)
                })
        
        except Exception as e:
            print(f"WebSocket error: {e}")
            break

def extract_code_blocks(markdown_text):
    """Extract code blocks from markdown text"""
    import re
    
    # Regular expression to match code blocks
    code_block_pattern = r"```(\w*)\n(.*?)```"
    code_blocks = []
    
    # Find all code blocks
    matches = re.finditer(code_block_pattern, markdown_text, re.DOTALL)
    for match in matches:
        language = match.group(1).strip()
        code = match.group(2).strip()
        
        if code:
            code_blocks.append({
                "language": language,
                "code": code
            })
    
    return code_blocks

@app.route("/api/create-artifact", methods=["POST"])
@login_required
async def create_artifact():
    """API endpoint to manually create an artifact"""
    user_id = current_user.user_id
    data = await request.json
    
    chat_id = data.get("chat_id")
    title = data.get("title")
    content = data.get("content")
    language = data.get("language", "")
    content_type = data.get("content_type", "text/plain")
    
    # Validate inputs
    if not all([chat_id, title, content]):
        return jsonify({"success": False, "error": "Missing required fields"}), 400
    
    # Verify chat belongs to user
    async with db_pool.acquire() as conn:
        chat = await conn.fetchrow(
            "SELECT id FROM chats WHERE id = $1 AND user_id = $2",
            chat_id, user_id
        )
        
        if not chat:
            return jsonify({"success": False, "error": "Chat not found"}), 404
        
        # Create artifact
        artifact_id = await conn.fetchval(
            """
            INSERT INTO artifacts 
            (chat_id, title, content, content_type, language) 
            VALUES ($1, $2, $3, $4, $5) 
            RETURNING id
            """,
            chat_id, title, content, content_type, language
        )
    
    return jsonify({
        "success": True,
        "id": artifact_id,
        "title": title,
        "language": language,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

@app.route("/api/download-artifact/<int:artifact_id>")
@login_required
async def download_artifact(artifact_id):
    """Download an artifact as a file"""
    user_id = current_user.user_id
    
    async with db_pool.acquire() as conn:
        # Check if artifact exists and belongs to user
        artifact = await conn.fetchrow(
            """
            SELECT a.id, a.title, a.content, a.content_type, a.language
            FROM artifacts a
            JOIN chats c ON a.chat_id = c.id
            WHERE a.id = $1 AND c.user_id = $2
            """,
            artifact_id, user_id
        )
        
        if not artifact:
            return jsonify({"success": False, "error": "Artifact not found"}), 404
    
    # Generate filename
    filename = slugify(artifact["title"])
    
    # Add appropriate extension based on language or content type
    if artifact["language"]:
        ext = get_extension_for_language(artifact["language"])
        if ext and not filename.endswith(ext):
            filename = f"{filename}{ext}"
    
    # Set appropriate headers for file download
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    
    return Response(
        artifact["content"],
        headers=headers,
        content_type=artifact["content_type"] or "text/plain"
    )

def get_extension_for_language(language):
    """Get file extension for language"""
    extension_map = {
        "python": ".py",
        "javascript": ".js",
        "typescript": ".ts",
        "html": ".html",
        "css": ".css",
        "java": ".java",
        "c": ".c",
        "cpp": ".cpp",
        "csharp": ".cs",
        "go": ".go",
        "rust": ".rs",
        "php": ".php",
        "ruby": ".rb",
        "swift": ".swift",
        "kotlin": ".kt",
        "scala": ".scala",
        "shell": ".sh",
        "bash": ".sh",
        "sql": ".sql",
        "json": ".json",
        "xml": ".xml",
        "yaml": ".yml",
        "markdown": ".md",
        "text": ".txt",
    }
    return extension_map.get(language.lower(), "")

@app.route("/register", methods=["GET", "POST"])
async def register():
    """User registration page - only accessible to admin users"""
    # Check if already logged in
    if await current_user.is_authenticated:
        user_id = current_user.user_id
        async with db_pool.acquire() as conn:
            is_admin = await conn.fetchval(
                "SELECT is_admin FROM users WHERE id = $1", 
                user_id
            )
            
            if not is_admin:
                return redirect(url_for("index"))
    else:
        return redirect(url_for("login"))
    
    error = None
    success = None
    
    if request.method == "POST":
        form = await request.form
        username = form.get("username", "").strip()
        password = form.get("password", "")
        confirm_password = form.get("confirm_password", "")
        email = form.get("email", "").strip()
        full_name = form.get("full_name", "").strip()
        is_admin = "is_admin" in form
        
        if not username or not password:
            error = "Username and password are required"
        elif password != confirm_password:
            error = "Passwords do not match"
        else:
            # Check if username already exists
            async with db_pool.acquire() as conn:
                existing_user = await conn.fetchval(
                    "SELECT id FROM users WHERE username = $1",
                    username
                )
                
                if existing_user:
                    error = "Username already exists"
                else:
                    # Create new user
                    await conn.execute(
                        """
                        INSERT INTO users (username, password, email, full_name, is_admin)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        username,
                        password,
                        email,
                        full_name,
                        is_admin
                    )
                    
                    success = f"User {username} created successfully"
    
    # Get list of existing users
    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            """
            SELECT id, username, email, full_name, is_admin, created_at, last_login
            FROM users
            ORDER BY username
            """
        )
        
        formatted_users = []
        for user in users:
            formatted_users.append({
                "id": user["id"],
                "username": user["username"],
                "email": user["email"],
                "full_name": user["full_name"],
                "is_admin": user["is_admin"],
                "created_at": user["created_at"].strftime("%Y-%m-%d %H:%M") if user["created_at"] else "",
                "last_login": user["last_login"].strftime("%Y-%m-%d %H:%M") if user["last_login"] else "Never"
            })
    
    return await render_template(
        "register.html",
        error=error,
        success=success,
        users=formatted_users,
        user=user
    )

@app.route("/api/delete-user/<int:user_id>", methods=["POST"])
@login_required
async def delete_user(user_id):
    """Delete a user - admin only"""
    current_uid = current_user.user_id
    
    # Check if current user is admin
    async with db_pool.acquire() as conn:
        is_admin = await conn.fetchval(
            "SELECT is_admin FROM users WHERE id = $1",
            current_uid
        )
        
        if not is_admin:
            return jsonify({"success": False, "error": "Unauthorized"}), 403
        
        # Cannot delete yourself
        if user_id == current_uid:
            return jsonify({"success": False, "error": "Cannot delete yourself"}), 400
        
        # Delete user
        await conn.execute(
            "DELETE FROM users WHERE id = $1",
            user_id
        )
    
    return jsonify({"success": True})

@app.route("/user/settings", methods=["GET", "POST"])
@login_required
async def user_settings():
    """User settings page"""
    user_id = current_user.user_id
    error = None
    success = None
    
    # Get user data
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, username, email, full_name, is_admin
            FROM users
            WHERE id = $1
            """,
            user_id
        )
    
    if request.method == "POST":
        form = await request.form
        action = form.get("action")
        
        if action == "update_profile":
            # Update profile information
            email = form.get("email", "").strip()
            full_name = form.get("full_name", "").strip()
            
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE users
                    SET email = $1, full_name = $2
                    WHERE id = $3
                    """,
                    email, full_name, user_id
                )
                success = "Profile updated successfully"
                
                # Update local user data
                user = dict(user)
                user["email"] = email
                user["full_name"] = full_name
                
        elif action == "change_password":
            # Change password
            current_password = form.get("current_password")
            new_password = form.get("new_password")
            confirm_password = form.get("confirm_password")
            
            if not all([current_password, new_password, confirm_password]):
                error = "All password fields are required"
            elif new_password != confirm_password:
                error = "New passwords do not match"
            else:
                async with db_pool.acquire() as conn:
                    password = await conn.fetchval(
                        "SELECT password FROM users WHERE id = $1",
                        user_id
                    )
                    
                    if password == current_password:
                        # Update password
                        await conn.execute(
                            """
                            UPDATE users
                            SET password = $1
                            WHERE id = $2
                            """,
                            new_password,
                            user_id
                        )
                        success = "Password changed successfully"
                    else:
                        error = "Current password is incorrect"
    
    return await render_template(
        "settings.html",
        user=user,
        error=error,
        success=success
    )

@app.route("/api/sessions")
@login_required
async def list_sessions():
    """List active sessions for current user"""
    user_id = current_user.user_id
    
    async with db_pool.acquire() as conn:
        sessions = await conn.fetch(
            """
            SELECT session_id, created_at, expires_at, ip_address, user_agent
            FROM sessions
            WHERE user_id = $1 AND expires_at > CURRENT_TIMESTAMP
            ORDER BY created_at DESC
            """,
            user_id
        )
        
        formatted_sessions = []
        for session_data in sessions:
            formatted_sessions.append({
                "id": session_data["session_id"],
                "created_at": session_data["created_at"].strftime("%Y-%m-%d %H:%M"),
                "expires_at": session_data["expires_at"].strftime("%Y-%m-%d %H:%M"),
                "ip_address": session_data["ip_address"],
                "user_agent": session_data["user_agent"],
                "is_current": session_data["session_id"] == session.get("session_id")
            })
    
    return jsonify({
        "success": True,
        "sessions": formatted_sessions
    })

@app.route("/api/sessions/revoke", methods=["POST"])
@login_required
async def revoke_session():
    """Revoke a session"""
    user_id = current_user.user_id
    data = await request.json
    session_id = data.get("session_id")
    
    if not session_id:
        return jsonify({"success": False, "error": "Session ID required"}), 400
    
    # Can't revoke current session through this endpoint
    if session_id == session.get("session_id"):
        return jsonify({"success": False, "error": "Cannot revoke current session"}), 400
        
    # Revoke the session
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM sessions WHERE session_id = $1 AND user_id = $2",
            session_id, user_id
        )
    
    return jsonify({"success": True})

@app.route("/api/revoke-all-sessions", methods=["POST"])
@login_required
async def revoke_all_sessions():
    """Revoke all sessions except current one"""
    user_id = current_user.user_id
    current_session_id = session.get("session_id")
    
    if not current_session_id:
        return jsonify({"success": False, "error": "Current session not found"}), 400
    
    # Revoke all other sessions
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM sessions 
            WHERE user_id = $1 AND session_id != $2
            """,
            user_id, current_session_id
        )
    
    return jsonify({"success": True})

@app.route("/chat/<int:chat_id>")
@login_required
async def chat(chat_id):
    """Chat page - show a specific chat"""
    user_id = current_user.user_id
    
    # Check if chat exists and belongs to user
    async with db_pool.acquire() as conn:
        chat = await conn.fetchrow(
            """
            SELECT id, title, created_at, updated_at
            FROM chats
            WHERE id = $1 AND user_id = $2
            """,
            chat_id, user_id
        )
        
        if not chat:
            return redirect(url_for("index"))
        
        # Get user info
        user = await conn.fetchrow(
            "SELECT username, full_name, is_admin FROM users WHERE id = $1",
            user_id
        )
        
        # Get chat messages
        messages = await conn.fetch(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE chat_id = $1
            ORDER BY created_at
            """,
            chat_id
        )
        
        # Format messages
        formatted_messages = []
        for message in messages:
            formatted_messages.append({
                "id": message["id"],
                "role": message["role"],
                "content": message["content"],
                "timestamp": message["created_at"].strftime("%H:%M")
            })
        
        # Get artifacts
        artifacts = await conn.fetch(
            """
            SELECT id, title, language, created_at
            FROM artifacts
            WHERE chat_id = $1
            ORDER BY created_at
            """,
            chat_id
        )
        
        # Format artifacts
        formatted_artifacts = []
        for artifact in artifacts:
            formatted_artifacts.append({
                "id": artifact["id"],
                "title": artifact["title"],
                "language": artifact["language"],
                "created_at": artifact["created_at"].strftime("%Y-%m-%d %H:%M")
            })
    
    # Format chat data
    formatted_chat = {
        "id": chat["id"],
        "title": chat["title"],
        "created_at": chat["created_at"].strftime("%Y-%m-%d %H:%M")
    }
    
    return await render_template(
        "chat.html",
        user=user,
        chat=formatted_chat,
        messages=formatted_messages,
        artifacts=formatted_artifacts,
        model=session.get("selected_model", MODEL_NAME)
    )

@app.route("/new-chat", methods=["POST"])
@login_required
async def new_chat():
    """Create a new chat"""
    user_id = current_user.user_id
    
    # Get selected model
    form = await request.form
    model = form.get("model", MODEL_NAME)
    
    # Store selected model in session
    session["selected_model"] = model
    
    # Create a new chat
    async with db_pool.acquire() as conn:
        chat_id = await conn.fetchval(
            """
            INSERT INTO chats (user_id, title)
            VALUES ($1, $2)
            RETURNING id
            """,
            user_id, "New Chat"
        )
    
    return redirect(url_for("chat", chat_id=chat_id))

@app.route("/chat/<int:chat_id>/update-title", methods=["POST"])
@login_required
async def update_chat_title(chat_id):
    """Update chat title"""
    user_id = current_user.user_id
    data = await request.json
    new_title = data.get("title", "").strip()
    
    if not new_title:
        return jsonify({"success": False, "error": "Title cannot be empty"}), 400
    
    # Update title in database
    async with db_pool.acquire() as conn:
        # Check if chat belongs to user
        chat = await conn.fetchrow(
            "SELECT id FROM chats WHERE id = $1 AND user_id = $2",
            chat_id, user_id
        )
        
        if not chat:
            return jsonify({"success": False, "error": "Chat not found"}), 404
        
        # Update the title
        await conn.execute(
            "UPDATE chats SET title = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
            new_title, chat_id
        )
    
    return jsonify({"success": True})

@app.route("/chat/<int:chat_id>/archive", methods=["POST"])
@login_required
async def archive_chat(chat_id):
    """Archive a chat"""
    user_id = current_user.user_id
    
    # Mark chat as archived
    async with db_pool.acquire() as conn:
        # Check if chat belongs to user
        chat = await conn.fetchrow(
            "SELECT id FROM chats WHERE id = $1 AND user_id = $2",
            chat_id, user_id
        )
        
        if not chat:
            return jsonify({"success": False, "error": "Chat not found"}), 404
        
        # Archive the chat
        await conn.execute(
            "UPDATE chats SET is_archived = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = $1",
            chat_id
        )
    
    return jsonify({"success": True})

@app.route("/archived-chats")
@login_required
async def archived_chats():
    """Show archived chats"""
    user_id = current_user.user_id
    
    async with db_pool.acquire() as conn:
        # Get user info
        user = await conn.fetchrow(
            "SELECT username, full_name, is_admin FROM users WHERE id = $1",
            user_id
        )
        
        # Get archived chats
        chats = await conn.fetch(
            """
            SELECT id, title, created_at, updated_at 
            FROM chats 
            WHERE user_id = $1 AND is_archived 
            ORDER BY updated_at DESC
            """,
            user_id
        )
        
        # Format dates
        formatted_chats = []
        for chat in chats:
            formatted_chats.append({
                "id": chat["id"],
                "title": chat["title"],
                "created_at": chat["created_at"].strftime("%Y-%m-%d %H:%M"),
                "updated_at": chat["updated_at"].strftime("%Y-%m-%d %H:%M")
            })
    
    return await render_template(
        "archived.html",
        user=user,
        chats=formatted_chats
    )

@app.route("/chat/<int:chat_id>/restore", methods=["POST"])
@login_required
async def restore_chat(chat_id):
    """Restore an archived chat"""
    user_id = current_user.user_id
    
    # Mark chat as not archived
    async with db_pool.acquire() as conn:
        # Check if chat belongs to user
        chat = await conn.fetchrow(
            "SELECT id FROM chats WHERE id = $1 AND user_id = $2",
            chat_id, user_id
        )
        
        if not chat:
            return jsonify({"success": False, "error": "Chat not found"}), 404
        
        # Restore the chat
        await conn.execute(
            "UPDATE chats SET is_archived = FALSE, updated_at = CURRENT_TIMESTAMP WHERE id = $1",
            chat_id
        )
    
    return jsonify({"success": True})

@app.route("/chat/<int:chat_id>/delete", methods=["POST"])
@login_required
async def delete_chat(chat_id):
    """Permanently delete a chat"""
    user_id = current_user.user_id
    
    # Delete chat and all associated data
    async with db_pool.acquire() as conn:
        # Check if chat belongs to user
        chat = await conn.fetchrow(
            "SELECT id FROM chats WHERE id = $1 AND user_id = $2",
            chat_id, user_id
        )
        
        if not chat:
            return jsonify({"success": False, "error": "Chat not found"}), 404
        
        # Delete all related data first (cascading delete would be better in actual DB schema)
        await conn.execute("DELETE FROM artifacts WHERE chat_id = $1", chat_id)
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        
        # Delete the chat
        await conn.execute("DELETE FROM chats WHERE id = $1", chat_id)
    
    return jsonify({"success": True})

@app.route("/github-import", methods=["GET", "POST"])
@login_required
async def github_import():
    """Page to import code from GitHub"""
    user_id = current_user.user_id
    error = None
    success = None
    
    # Get user info
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT username, full_name, is_admin FROM users WHERE id = $1",
            user_id
        )
    
    if request.method == "POST":
        form = await request.form
        github_url = form.get("github_url", "").strip()
        chat_title = form.get("chat_title", "").strip()
        
        if not github_url:
            error = "GitHub URL is required"
        elif not is_valid_github_url(github_url):
            error = "Invalid GitHub URL format"
        else:
            # Create a new chat for the GitHub import
            if not chat_title:
                # Generate a title based on the URL
                chat_title = generate_title_from_github_url(github_url)
            
            # Create a new chat
            async with db_pool.acquire() as conn:
                chat_id = await conn.fetchval(
                    """
                    INSERT INTO chats (user_id, title)
                    VALUES ($1, $2)
                    RETURNING id
                    """,
                    user_id, chat_title
                )
            
            # Redirect to process this in the background via websocket
            return redirect(url_for("github_process", chat_id=chat_id, url=github_url))
    
    return await render_template(
        "github_import.html",
        user=user,
        error=error,
        success=success
    )

@app.route("/github-process/<int:chat_id>")
@login_required
async def github_process(chat_id):
    """Page to process a GitHub URL import into an existing chat"""
    user_id = current_user.user_id
    github_url = request.args.get("url", "")
    
    # Verify that the chat belongs to the user
    async with db_pool.acquire() as conn:
        chat = await conn.fetchrow(
            "SELECT id, title FROM chats WHERE id = $1 AND user_id = $2",
            chat_id, user_id
        )
        
        if not chat:
            return redirect(url_for("index"))
        
        # Get user info
        user = await conn.fetchrow(
            "SELECT username, full_name, is_admin FROM users WHERE id = $1",
            user_id
        )
    
    if not github_url:
        return redirect(url_for("chat", chat_id=chat_id))
    
    return await render_template(
        "github_process.html",
        user=user,
        chat=chat,
        github_url=github_url
    )

@app.websocket("/ws/github/<int:chat_id>")
@login_required
async def ws_github(chat_id):
    """WebSocket endpoint for GitHub import processing"""
    user_id = current_user.user_id
    
    # Verify that the chat belongs to the user
    async with db_pool.acquire() as conn:
        chat = await conn.fetchrow(
            "SELECT id FROM chats WHERE id = $1 AND user_id = $2",
            chat_id, user_id
        )
        
        if not chat:
            return
    
    await websocket.accept()
    
    try:
        # Get the GitHub URL from the client
        message_data = await websocket.receive_json()
        github_url = message_data.get("github_url", "").strip()
        
        if not github_url or not is_valid_github_url(github_url):
            await websocket.send_json({
                "type": "error",
                "error": "Invalid GitHub URL"
            })
            return
        
        # Send status update
        await websocket.send_json({
            "type": "status",
            "message": "Connecting to GitHub..."
        })
        
        # Initialize GitHub connector with optional token
        github = GithubConnector(access_token=GITHUB_TOKEN if GITHUB_TOKEN else None)
        
        try:
            # Parse the GitHub URL
            await websocket.send_json({
                "type": "status",
                "message": "Parsing GitHub URL..."
            })
            
            parsed_url = await github.parse_github_url(github_url)
            
            # Send status update
            await websocket.send_json({
                "type": "status",
                "message": f"Fetching content from GitHub: {parsed_url['owner']}/{parsed_url['repo']}..."
            })
            
            # Fetch content from GitHub
            result = await github.fetch_from_url(github_url, max_files=30)
            
            if "error" in result:
                await websocket.send_json({
                    "type": "error",
                    "error": result["error"]
                })
                return
            
            # Send status update
            await websocket.send_json({
                "type": "status",
                "message": "Processing repository content..."
            })
            
            # Get repository summary
            repo_summary = await github.get_repository_summary(
                parsed_url["owner"],
                parsed_url["repo"]
            )
            
            # Format summary for LLM
            summary_text = await summarize_repository_for_llm(repo_summary)
            
            # Format code for LLM with size limit
            code_text = await format_code_for_llm(result["content"], context_limit=12000)
            
            # Create initial message with repository summary
            async with db_pool.acquire() as conn:
                user_msg_id = await conn.fetchval(
                    """
                    INSERT INTO messages (chat_id, role, content) 
                    VALUES ($1, $2, $3) 
                    RETURNING id
                    """,
                    chat_id, "user", f"Import from GitHub: {github_url}\n\n{summary_text}"
                )
            
            # Send confirmation of user message
            await websocket.send_json({
                "type": "message",
                "id": user_msg_id,
                "role": "user",
                "content": f"Import from GitHub: {github_url}",
                "timestamp": datetime.now().strftime("%H:%M")
            })
            
            # Send status update
            await websocket.send_json({
                "type": "status",
                "message": "Sending to DeepSeek-Coder for analysis..."
            })
            
            # Prepare system prompt
            system_prompt = """You are DeepSeek-Coder, an AI assistant specialized in code analysis. 
            You are examining a GitHub repository that has been imported. 
            Please analyze the code and provide a detailed summary including:
            1. The overall purpose of the repository
            2. Key components and their functionality
            3. The architecture and how components interact
            4. Any notable patterns, libraries, or technologies used
            5. Potential areas for improvement or issues
            
            Be thorough yet concise in your analysis."""
            
            # Build the prompt
            prompt = system_prompt + "\n\n"
            prompt += f"GitHub Repository Information:\n{summary_text}\n\n"
            prompt += f"Code Files:\n{code_text}\n\n"
            prompt += "Please provide a comprehensive analysis of this repository:"
            
            # Get currently selected model
            model = session.get("selected_model", MODEL_NAME)
            
            # Send to Ollama for analysis
            try:
                # Stream response from Ollama
                async with httpx.AsyncClient(timeout=300.0) as client:  # Increase timeout for large repos
                    response = await client.post(
                        f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/generate",
                        json={
                            "model": model,
                            "prompt": prompt,
                            "stream": True
                        },
                        headers={"Content-Type": "application/json"},
                    )
                    
                    # Create assistant message in database
                    async with db_pool.acquire() as conn:
                        assistant_msg_id = await conn.fetchval(
                            """
                            INSERT INTO messages (chat_id, role, content) 
                            VALUES ($1, $2, $3) 
                            RETURNING id
                            """,
                            chat_id, "assistant", ""
                        )
                    
                    # Start with empty response
                    full_response = ""
                    
                    # Process streaming response
                    buffer = b""
                    async for chunk in response.aiter_bytes():
                        buffer += chunk
                        
                        # Process complete JSON objects
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if line.strip():
                                try:
                                    chunk_data = json.loads(line)
                                    if "response" in chunk_data:
                                        token = chunk_data["response"]
                                        full_response += token
                                        
                                        # Send token to client
                                        await websocket.send_json({
                                            "type": "token",
                                            "token": token,
                                            "id": assistant_msg_id
                                        })
                                        
                                        # If done flag is True, break
                                        if chunk_data.get("done", False):
                                            break
                                except json.JSONDecodeError:
                                    continue
                    
                    # Update the complete message in the database
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE messages SET content = $1 WHERE id = $2",
                            full_response, assistant_msg_id
                        )
                    
                    # Store repository files as artifacts
                    for file_path, file_info in result["content"].items():
                        if isinstance(file_info, dict) and "content" in file_info:
                            # Skip files that are too large or not text
                            if len(file_info["content"]) > 100000:  # Skip files larger than 100KB
                                continue
                            
                            # Only store actual code files
                            language = file_info.get("language", "")
                            if not language or language == "text":
                                continue
                            
                            # Create artifact title (use just the filename part)
                            filename = file_path.split("/")[-1]
                            title = f"{filename} (GitHub)"
                            
                            # Save artifact to database
                            async with db_pool.acquire() as conn:
                                artifact_id = await conn.fetchval(
                                    """
                                    INSERT INTO artifacts 
                                    (chat_id, message_id, title, content, content_type, language) 
                                    VALUES ($1, $2, $3, $4, $5, $6) 
                                    RETURNING id
                                    """,
                                    chat_id, assistant_msg_id, title,
                                    file_info["content"], "text/plain", language
                                )
                            
                            # Notify client about new artifact
                            await websocket.send_json({
                                "type": "artifact",
                                "id": artifact_id,
                                "title": title,
                                "language": language,
                                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
                            })
                    
                    # Send completion message
                    await websocket.send_json({
                        "type": "complete",
                        "id": assistant_msg_id,
                        "role": "assistant",
                        "content": full_response,
                        "timestamp": datetime.now().strftime("%H:%M")
                    })
                    
            except Exception as e:
                print(f"Error processing with Ollama: {e}")
                await websocket.send_json({
                    "type": "error",
                    "error": str(e)
                })
                
        except Exception as e:
            print(f"GitHub error: {e}")
            await websocket.send_json({
                "type": "error",
                "error": f"GitHub error: {str(e)}"
            })
        finally:
            # Cleanup
            github.cleanup()
            
    except Exception as e:
        print(f"WebSocket error: {e}")
        await websocket.send_json({
            "type": "error",
            "error": f"Error: {str(e)}"
        })

# Helper functions for GitHub URL processing
def is_valid_github_url(url):
    """Check if a URL is a valid GitHub URL"""
    try:
        parsed = urlparse(url)
        return (
            parsed.netloc == "github.com" and
            len(parsed.path.strip("/").split("/")) >= 2
        )
    except:
        return False

def generate_title_from_github_url(url):
    """Generate a chat title from a GitHub URL"""
    try:
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        
        if len(path_parts) >= 2:
            owner = path_parts[0]
            repo = path_parts[1]
            
            # Check if there's a specific file or directory
            if len(path_parts) > 3 and path_parts[2] in ("blob", "tree"):
                if len(path_parts) > 4:
                    path_suffix = "/".join(path_parts[4:])
                    if len(path_suffix) > 30:
                        path_suffix = path_suffix[:27] + "..."
                    return f"{owner}/{repo}: {path_suffix}"
                else:
                    return f"{owner}/{repo}"
            else:
                return f"{owner}/{repo}"
        else:
            return "GitHub Import"
    except:
        return "GitHub Import"

if __name__ == "__main__":
    config = Config()
    config.bind = ["0.0.0.0:8000"]
    config.workers = 1
    
    asyncio.run(serve(app, config))