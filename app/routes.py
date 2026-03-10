from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, abort, Response
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

        # 2. Запускаємо Docker контейнер
        client = docker.from_env()
        try:
            assigned_ports_db = {}

            # Якщо це Zomboid, він краще працює в режимі host network
            if template.name == 'Project Zomboid':
                # Записуємо стандартні порти в БД для відображення
                assigned_ports_db = {'16261': 16261, '16262': 16262}
                new_server.assigned_ports = assigned_ports_db

                container = client.containers.run(
                    image=template.docker_image,
                    detach=True,
                    name=f"server_{new_server.uuid}",
                    network_mode="host",  # ВАЖЛИВО! Контейнер використовує IP ноутбука
                    environment=template.default_env_vars,
                    mem_limit=f"{form.ram.data}m",
                    restart_policy={"Name": "on-failure"}
                )
            else:
                # Для Minecraft та інших ігор залишаємо стару логіку прокидання портів
                docker_ports = {}
                current_external_port = external_port

                for internal_port, protocol in template.default_ports.items():
                    port_key = f"{internal_port}/{protocol}"
                    docker_ports[port_key] = current_external_port
                    assigned_ports_db[internal_port] = current_external_port
                    current_external_port += 1

                new_server.assigned_ports = assigned_ports_db

                container = client.containers.run(
                    image=template.docker_image,
                    detach=True,
                    name=f"server_{new_server.uuid}",
                    ports=docker_ports,
                    environment=template.default_env_vars,
                    mem_limit=f"{form.ram.data}m",
                    restart_policy={"Name": "on-failure"}
                )

            new_server.status = 'running'
            db.session.commit()
            flash(f'Сервер {new_server.name} успішно запущено!', 'success')
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
            # PZ потребує 16261 (головний) та 16262 (direct connect) по UDP
            'ports': {'16261': 'udp', '16262': 'udp'},
            'env': {
                'ADMIN_PASSWORD': 'admin',
                'SERVER_NAME': 'MyZomboidServer',
                # Жорстко лімітуємо Java, щоб вона не виходила за межі контейнера
                'START_MEMORY': '2048m',
                'MAX_MEMORY': '4096m'
            }
        }
    ]

    added_count = 0
    for game in games:
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


# --- УПРАВЛІННЯ СЕРВЕРОМ ---

@bp.route('/server/<int:server_id>')
@login_required
def server_details(server_id):
    server = GameServer.query.get_or_404(server_id)
    # Перевірка прав доступу (щоб не лазили в чужі сервери)
    if server.owner_id != current_user.id:
        abort(403)
    return render_template('server_details.html', server=server)


@bp.route('/server/<int:server_id>/<action>')
@login_required
def server_action(server_id, action):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        abort(403)

    client = docker.from_env()
    container_name = f"server_{server.uuid}"

    try:
        container = client.containers.get(container_name)

        if action == 'start':
            container.start()
            server.status = 'running'
            flash('Сервер запускається...', 'success')
        elif action == 'stop':
            container.stop()
            server.status = 'stopped'
            flash('Сервер зупинено.', 'warning')
        elif action == 'restart':
            container.restart()
            server.status = 'running'
            flash('Сервер перезавантажено.', 'info')
        elif action == 'kill':
            container.kill()
            server.status = 'stopped'
            flash('Сервер примусово вбито.', 'danger')

        db.session.commit()

    except docker.errors.NotFound:
        server.status = 'error'
        db.session.commit()
        flash('Контейнер не знайдено! Можливо, він був видалений вручну.', 'danger')
    except Exception as e:
        flash(f'Помилка виконання дії: {e}', 'danger')

    return redirect(url_for('main.server_details', server_id=server.id))


@bp.route('/server/<int:server_id>/delete')
@login_required
def delete_server(server_id):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        abort(403)

    client = docker.from_env()
    container_name = f"server_{server.uuid}"

    # Спробуємо видалити контейнер
    try:
        try:
            container = client.containers.get(container_name)
            container.stop()
            container.remove()
        except docker.errors.NotFound:
            pass  # Якщо контейнера вже немає, просто видаляємо з БД

        db.session.delete(server)
        db.session.commit()
        flash('Сервер успішно видалено.', 'success')
    except Exception as e:
        flash(f'Помилка видалення: {e}', 'danger')

    return redirect(url_for('main.dashboard'))


@bp.route('/server/<int:server_id>/logs')
@login_required
def server_logs(server_id):
    """AJAX маршрут для отримання логів"""
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")
        # Отримуємо останні 100 рядків логів
        logs = container.logs(tail=100).decode('utf-8')
        return jsonify({'logs': logs, 'status': container.status})
    except Exception as e:
        return jsonify({'logs': f"Error fetching logs: {e}", 'status': 'unknown'})


@bp.route('/server/<int:server_id>/download_logs')
@login_required
def download_logs(server_id):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id:
        abort(403)

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")
        # Беремо всі логи з контейнера (не тільки останні 100)
        logs = container.logs().decode('utf-8')

        # Повертаємо текстовий файл "на льоту"
        return Response(
            logs,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename=server_{server.name}_logs.txt"}
        )
    except Exception as e:
        flash(f'Помилка завантаження логів: {e}', 'danger')
        return redirect(url_for('main.server_details', server_id=server.id))