import sqlite3
import json
from datetime import datetime
from pathlib import Path
import logging
from pydantic import BaseModel
from typing import Optional
import os
from functions.config_loader import get_config
import shutil

logger = logging.getLogger(__name__)

class DreamData(BaseModel):
    user_prompt: str
    generated_prompt: str
    audio_filename: str
    video_filename: str
    thumb_filename: Optional[str] = None
    status: Optional[str] = 'completed'

class DreamDB:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = get_config()['DB_PATH']
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize the database and create tables if they don't exist. If the dreams table is created, also initialize sample dreams."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Check if the dreams table exists
            cursor.execute("""
                SELECT name FROM sqlite_master WHERE type='table' AND name='dreams';
            """)
            table_exists = cursor.fetchone() is not None
            # Create the table if it doesn't exist
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dreams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_prompt TEXT NOT NULL,
                    generated_prompt TEXT NOT NULL,
                    audio_filename TEXT NOT NULL,
                    video_filename TEXT NOT NULL,
                    thumb_filename TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dream_transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dream_id INTEGER,
                    transcript TEXT NOT NULL,
                    audio_filename TEXT,
                    dream_local_date TEXT NOT NULL,
                    dream_local_time TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(dream_id) REFERENCES dreams(id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dayone_sync_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transcript_id INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    sync_status TEXT NOT NULL DEFAULT 'pending',
                    relay_job_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(transcript_id) REFERENCES dream_transcripts(id)
                )
            ''')
            conn.commit()
            # If the table did not exist before, initialize sample dreams
            if not table_exists:
                self._init_sample_dreams()

    def _init_sample_dreams(self):
        """Copy sample dreams and insert them into the database if missing."""
        SAMPLES_DIR = os.path.join(os.path.dirname(__file__), '..', 'dream_samples')
        VIDEO_DEST = os.path.join(os.path.dirname(__file__), '..', get_config()['VIDEOS_DIR'])
        THUMB_DEST = os.path.join(os.path.dirname(__file__), '..', get_config()['THUMBS_DIR'])
        SAMPLES = [
            {'video': 'video_1.mp4', 'thumb': 'thumb_1.png', 'video_dest': 'dream_1.mp4', 'thumb_dest': 'dream_1.png'},
            {'video': 'video_2.mp4', 'thumb': 'thumb_2.png', 'video_dest': 'dream_2.mp4', 'thumb_dest': 'dream_2.png'},
            {'video': 'video_3.mp4', 'thumb': 'thumb_3.png', 'video_dest': 'dream_3.mp4', 'thumb_dest': 'dream_3.png'},
            {'video': 'video_4.mp4', 'thumb': 'thumb_4.png', 'video_dest': 'dream_4.mp4', 'thumb_dest': 'dream_4.png'},
        ]
        # Ensure destination directories exist
        os.makedirs(VIDEO_DEST, exist_ok=True)
        os.makedirs(THUMB_DEST, exist_ok=True)
        # Get existing video filenames
        existing = self.get_all_dreams()
        existing_videos = {d['video_filename'] for d in existing}
        for i, sample in enumerate(SAMPLES, 1):
            # Copy video
            src_video = os.path.join(SAMPLES_DIR, sample['video'])
            dst_video = os.path.join(VIDEO_DEST, sample['video_dest'])
            if not os.path.exists(dst_video):
                try:
                    shutil.copy2(src_video, dst_video)
                except Exception as e:
                    if logger:
                        logger.warning(f"Could not copy sample video {src_video} to {dst_video}: {e}")
            # Copy thumb
            src_thumb = os.path.join(SAMPLES_DIR, sample['thumb'])
            dst_thumb = os.path.join(THUMB_DEST, sample['thumb_dest'])
            if not os.path.exists(dst_thumb):
                try:
                    shutil.copy2(src_thumb, dst_thumb)
                except Exception as e:
                    if logger:
                        logger.warning(f"Could not copy sample thumb {src_thumb} to {dst_thumb}: {e}")
            # Insert into DB if not present
            if sample['video_dest'] not in existing_videos:
                dream_data = DreamData(
                    user_prompt='',
                    generated_prompt='',
                    audio_filename='',
                    video_filename=sample['video_dest'],
                    thumb_filename=sample['thumb_dest'],
                    status='completed',
                )
                self.save_dream(dream_data.model_dump())
                if logger:
                    logger.info(f"Inserted sample dream {i}")
            else:
                if logger:
                    logger.info(f"Sample dream {i} already exists in DB")
    
    def save_dream(self, dream_data):
        """Save a new dream record to the database."""
        required_fields = ['user_prompt', 'generated_prompt', 'audio_filename', 'video_filename']
        for field in required_fields:
            if field not in dream_data:
                raise ValueError(f"Missing required field: {field}")
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO dreams (
                    user_prompt, generated_prompt, audio_filename, video_filename,
                    thumb_filename, status
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                dream_data['user_prompt'],
                dream_data['generated_prompt'],
                dream_data['audio_filename'],
                dream_data['video_filename'],
                dream_data.get('thumb_filename'),
                dream_data.get('status', 'completed')
            ))
            conn.commit()
            return cursor.lastrowid

    def save_dream_transcript(self, transcript, audio_filename=None, recorded_at=None, device_id=None):
        """Persist a raw transcript and create a Day One sync outbox job."""
        if not transcript or not transcript.strip():
            raise ValueError("transcript is required")

        recorded_at = recorded_at or datetime.now()
        device_id = device_id or get_config().get('DAYONE_DEVICE_ID', 'dream-recorder')
        dream_local_date = recorded_at.date().isoformat()
        dream_local_time = recorded_at.strftime('%H:%M')

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO dream_transcripts (
                    transcript, audio_filename, dream_local_date, dream_local_time
                ) VALUES (?, ?, ?, ?)
            ''', (
                transcript.strip(),
                audio_filename,
                dream_local_date,
                dream_local_time,
            ))
            transcript_id = cursor.lastrowid
            idempotency_key = f"{device_id}:{transcript_id}"
            cursor.execute('''
                INSERT INTO dayone_sync_jobs (
                    transcript_id, idempotency_key, sync_status
                ) VALUES (?, ?, 'pending')
            ''', (transcript_id, idempotency_key))
            job_id = cursor.lastrowid
            conn.commit()

        return {
            'transcript_id': transcript_id,
            'job_id': job_id,
            'idempotency_key': idempotency_key,
            'dream_local_date': dream_local_date,
            'dream_local_time': dream_local_time,
        }

    def link_transcript_to_dream(self, transcript_id, dream_id):
        """Associate a previously saved raw transcript with the generated dream row."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE dream_transcripts SET dream_id = ? WHERE id = ?',
                (dream_id, transcript_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_dayone_sync_jobs(self, statuses=('pending',), limit=None):
        """Return Day One outbox jobs joined with their transcript payloads."""
        placeholders = ', '.join('?' for _ in statuses)
        query = f'''
            SELECT
                j.id,
                j.transcript_id,
                j.idempotency_key,
                j.sync_status,
                j.relay_job_id,
                j.attempts,
                j.last_error,
                j.created_at,
                j.updated_at,
                t.transcript,
                t.audio_filename,
                t.dream_local_date,
                t.dream_local_time
            FROM dayone_sync_jobs j
            JOIN dream_transcripts t ON t.id = j.transcript_id
            WHERE j.sync_status IN ({placeholders})
            ORDER BY t.dream_local_date, t.dream_local_time, j.id
        '''
        values = list(statuses)
        if limit:
            query += ' LIMIT ?'
            values.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, values)
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def mark_dayone_sync_submitted(self, job_id, relay_job_id=None):
        return self._update_dayone_sync_job(
            job_id,
            {'sync_status': 'submitted', 'relay_job_id': relay_job_id, 'last_error': None},
        )

    def mark_dayone_sync_failed(self, job_id, error):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE dayone_sync_jobs
                SET sync_status = 'pending',
                    attempts = attempts + 1,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (str(error), job_id))
            conn.commit()
            return cursor.rowcount > 0

    def _update_dayone_sync_job(self, job_id, updates):
        set_clauses = []
        values = []
        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        set_clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.append(job_id)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE dayone_sync_jobs SET {', '.join(set_clauses)} WHERE id = ?",
                values,
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def get_dream(self, dream_id):
        """Get a single dream by ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dreams WHERE id = ?', (dream_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_dict(row)
            return None
    
    def get_all_dreams(self):
        """Get all dreams, ordered by creation date (newest first)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dreams ORDER BY created_at DESC')
            return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def update_dream(self, dream_id, updates):
        """Update an existing dream."""
        if not updates:
            return
        
        try:
            set_clauses = []
            values = []
            for key, value in updates.items():
                set_clauses.append(f"{key} = ?")
                values.append(value)
            
            values.append(dream_id)
            query = f"UPDATE dreams SET {', '.join(set_clauses)} WHERE id = ?"
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(query, values)
                    conn.commit()
                    return cursor.rowcount > 0
                except sqlite3.Error as e:
                    if logger:
                        logger.error(f"Database error: {str(e)}")
                    if logger:
                        logger.error(f"Query: {query}")
                    if logger:
                        logger.error(f"Values: {values}")
                    raise
        except Exception as e:
            if logger:
                logger.error(f"Error updating dream {dream_id}: {str(e)}")
            raise
    
    def delete_dream(self, dream_id):
        """Delete a dream from the database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dreams WHERE id = ?', (dream_id,))
            conn.commit()
            return cursor.rowcount > 0
    
    def _row_to_dict(self, row):
        """Convert a database row to a dictionary."""
        return dict(row) 
