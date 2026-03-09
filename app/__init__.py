from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from config import Config

# Створюємо об'єкт бази даних
db = SQLAlchemy()
login_manager = LoginManager() # <--- Створили менеджер
login_manager.login_view = 'login'

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Ініціалізуємо БД
    db.init_app(app)
    login_manager.init_app(app)


    with app.app_context():
        from . import models
        from . import routes

        # Створюємо таблиці в БД автоматично (для диплому це ок)
        # У реальному проді використовують Flask-Migrate (Alembic)
        db.create_all()

    return app  