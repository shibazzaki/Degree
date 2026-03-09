import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'super-secret-key-for-diploma'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'postgresql://admin:admin_pass@db:5432/panel_db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False