from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask import current_app as app
from . import db, login_manager
from .models import User, GameServer, GameTemplate, db
from .forms import LoginForm, RegistrationForm, CreateServerForm
import docker
import socket
import random

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def find_free_port():
    # Знаходить вільний порт на хості (спрощена версія алгоритму з диплома)
    while True:
        port = random.randint(25500, 26000)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        if result != 0: # Порт вільний
            return port

# Функція, яка завантажує користувача з БД для сесії
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- МАРШРУТИ ---
# Перенаправлення з головної на дашборд
@app.route('/')
def index():
    # Якщо користувач залогінений - показуємо його сервери
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    else:
        return redirect(url_for('login'))


@app.route('/create', methods=['GET', 'POST'])
@login_required
def create_server():
    form = CreateServerForm()
    # Завантажуємо список ігор з БД у випадаючий список
    form.game_id.choices = [(g.id, g.name) for g in GameTemplate.query.all()]

    if form.validate_on_submit():
        template = GameTemplate.query.get(form.game_id.data)
        external_port = find_free_port()

        # 1. Створюємо запис у БД
        new_server = GameServer(
            name=form.name.data,
            owner_id=current_user.id,
            template_id=template.id,
            allocated_ram=form.ram.data,
            assigned_ports={'main': external_port},  # Зберігаємо зовнішній порт
            status='starting'
        )
        db.session.add(new_server)
        db.session.commit()  # Треба коміт, щоб отримати new_server.uuid

        # 2. Запускаємо Docker контейнер
        client = docker.from_env()
        try:
            # Отримуємо внутрішній порт (наприклад 25565 для Minecraft)
            internal_port = list(template.default_ports.keys())[0]

            container = client.containers.run(
                image=template.docker_image,
                detach=True,
                name=f"server_{new_server.uuid}",  # Унікальне ім'я контейнера
                ports={f"{internal_port}/tcp": external_port},  # Прокидаємо порти
                environment=template.default_env_vars,
                mem_limit=f"{form.ram.data}m",  # Ліміт RAM
                restart_policy={"Name": "on-failure"}
            )

            new_server.status = 'running'
            db.session.commit()
            flash(f'Сервер {new_server.name} успішно запущено на порті {external_port}!', 'success')
            return redirect(url_for('dashboard'))

        except Exception as e:
            db.session.delete(new_server)  # Відкочуємо БД, якщо докер впав
            db.session.commit()
            flash(f'Помилка запуску Docker: {e}', 'danger')

    return render_template('create.html', form=form)

@app.route('/seed_db')
def seed_db():
    # Додаємо шаблон Minecraft, якщо його немає
    if not GameTemplate.query.filter_by(name='Minecraft Java').first():
        minecraft = GameTemplate(
            name='Minecraft Java',
            docker_image='itzg/minecraft-server',
            default_ports={'25565': 'tcp'},
            default_env_vars={'EULA': 'TRUE', 'VERSION': 'LATEST'}
        )
        db.session.add(minecraft)
        db.session.commit()
        return "Minecraft template added!"
    return "Template already exists."


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        # Перевірка пароля (поки що без хешування для тесту, або з хешуванням якщо вже готово)
        if user and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Невірний логін або пароль')

    return render_template('login.html', form=form)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = RegistrationForm()
    if form.validate_on_submit():
        # Хешуємо пароль перед збереженням
        hashed_password = generate_password_hash(form.password.data)
        new_user = User(username=form.username.data, email=form.email.data, password_hash=hashed_password)

        try:
            db.session.add(new_user)
            db.session.commit()
            flash('Акаунт створено! Тепер увійдіть.')
            return redirect(url_for('login'))
        except:
            flash('Такий користувач або email вже існує.')

    return render_template('register.html', form=form)

@app.route('/dashboard')
@login_required
def dashboard():
    servers = GameServer.query.filter_by(owner_id=current_user.id).all()
    return render_template('dashboard.html', servers=servers)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))