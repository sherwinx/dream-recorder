#!/usr/bin/env python3
"""
Dream Recorder Demo Application
Simplified version for demo purposes - cycles through logo, icons, and clock
"""

import os
import logging
from flask import Flask, render_template
from flask_socketio import SocketIO
from datetime import datetime
import glob

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask App Initialization
app = Flask(__name__)
app.config.update(
    DEBUG=False,
    HOST='0.0.0.0',
    PORT=5000
)

# Initialize SocketIO without gevent
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

def get_total_background_images():
    """Count the total number of background images available."""
    background_dir = os.path.join(app.static_folder, 'images', 'background')
    if os.path.exists(background_dir):
        # Count all .jpg files in the background directory
        jpg_files = glob.glob(os.path.join(background_dir, '*.jpg'))
        return len(jpg_files)
    return 0

@app.route('/')
def index():
    """Main demo page."""
    total_images = get_total_background_images()
    logger.info(f"Total background images found: {total_images}")
    return render_template('demo.html', total_background_images=total_images)

@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.info('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.info('Client disconnected')

if __name__ == '__main__':
    logger.info("Starting Dream Recorder Demo Application")
    socketio.run(app, host=app.config['HOST'], port=app.config['PORT'], debug=app.config['DEBUG'], allow_unsafe_werkzeug=True)
