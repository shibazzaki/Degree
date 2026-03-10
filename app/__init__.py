from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
# ВАЖЛИВО: Оновлюємо назву view для логіну (додаємо main.)
login_manager.login_view = 'main.login'


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

    with app.app_context():
        from . import models

        # Імпортуємо та реєструємо Blueprint
        from .routes import bp as main_bp
        app.register_blueprint(main_bp)

        db.create_all()

    return app