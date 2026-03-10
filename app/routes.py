from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from . import db, login_manager
from .models import User, GameServer, GameTemplate
from .forms import LoginForm, RegistrationForm, CreateServerForm
import docker
import socket
import random

# Створюємо Blueprint замість app
bp = Blueprint('main', __name__)


# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def find_free_port():
    # Знаходить вільний порт на хості
    while True:
        port = random.randint(25500, 26000)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        if result != 0:  # Порт вільний
            return port


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --- МАРШРУТИ ---

@bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))  # <--- Зверни увагу на 'main.'
    else:
        return redirect(url_for('main.login'))


@bp.route('/create', methods=['GET', 'POST'])
@login_required
def create_server():
    form = CreateServerForm()
    form.game_id.choices = [(g.id, g.name) for g in GameTemplate.query.all()]

    if form.validate_on_submit():
        template = GameTemplate.query.get(form.game_id.data)
        external_port = find_free_port()

        new_server = GameServer(
            name=form.name.data,
            owner_id=current_user.id,
            template_id=template.id,
            allocated_ram=form.ram.data,
            assigned_ports={'main': external_port},
            status='starting'
        )
        db.session.add(new_server)
        db.session.commit()

        client = docker.from_env()
        try:
            internal_port = list(template.default_ports.keys())[0]

            # Важливо: network_mode="host" або прокидання портів.
            # Для простоти поки залишаємо ports.
            container = client.containers.run(
                image=template.docker_image,
                detach=True,
                name=f"server_{new_server.uuid}",
                ports={f"{internal_port}/tcp": external_port},
                environment=template.default_env_vars,
                mem_limit=f"{form.ram.data}m",
                restart_policy={"Name": "on-failure"}
            )

            new_server.status = 'running'
            db.session.commit()
            flash(f'Сервер {new_server.name} успішно запущено на порті {external_port}!', 'success')
            return redirect(url_for('main.dashboard'))

        except Exception as e:
            db.session.delete(new_server)
            db.session.commit()
            flash(f'Помилка запуску Docker: {e}', 'danger')

    return render_template('create.html', form=form)


@bp.route('/seed_db')
def seed_db():
    games = [
        {
            'name': 'Minecraft Java',
            'docker_image': 'itzg/minecraft-server',
            'ports': {'25565': 'tcp'},
            'env': {'EULA': 'TRUE', 'VERSION': 'LATEST'}
        },
        {
            'name': 'Project Zomboid',
            'docker_image': 'renegademaster/zomboid-dedicated-server',
            'ports': {'16261': 'udp'},  # Zomboid використовує UDP
            'env': {'ADMIN_PASSWORD': 'admin', 'SERVER_NAME': 'MyZomboidServer'}
        }
    ]

    added_count = 0
    for game in games:
        # Перевіряємо, чи існує вже такий шаблон
        if not GameTemplate.query.filter_by(name=game['name']).first():
            new_template = GameTemplate(
                name=game['name'],
                docker_image=game['docker_image'],
                default_ports=game['ports'],
                default_env_vars=game['env']
            )
            db.session.add(new_template)
            added_count += 1

    db.session.commit()
    return f"Базу оновлено! Додано нових шаблонів: {added_count}. <br><a href='{url_for('main.create_server')}'>Повернутись до створення</a>"


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            return redirect(url_for('main.index'))
        else:
            flash('Невірний логін або пароль')

    return render_template('login.html', form=form)


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = generate_password_hash(form.password.data)
        new_user = User(username=form.username.data, email=form.email.data, password_hash=hashed_password)

        try:
            db.session.add(new_user)
            db.session.commit()
            flash('Акаунт створено! Тепер увійдіть.')
            return redirect(url_for('main.login'))
        except:
            flash('Такий користувач або email вже існує.')

    return render_template('register.html', form=form)


@bp.route('/dashboard')
@login_required
def dashboard():
    servers = GameServer.query.filter_by(owner_id=current_user.id).all()
    return render_template('dashboard.html', servers=servers)


@bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.login'))