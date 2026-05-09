from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, abort, Response, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from . import db, login_manager
from .models import User, GameServer, GameTemplate
from .forms import LoginForm, RegistrationForm, CreateServerForm
import docker
import socket
import random
from mcrcon import MCRcon
import os
import requests
import base64
from functools import wraps


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


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.role != 'admin':
            abort(403) # Повертає помилку 403 Forbidden
        return f(*args, **kwargs)
    return decorated_function

# --- МАРШРУТИ ---
@bp.app_context_processor
def inject_public_ip():
    return dict(public_ip=current_app.config['PUBLIC_IP'])

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

        # 1. Створюємо запис у БД
        new_server = GameServer(
            name=form.name.data,
            owner_id=current_user.id,
            template_id=template.id,
            allocated_ram=form.ram.data,
            assigned_ports={'main': external_port},  # Тимчасовий запис
            env_vars=template.default_env_vars,  # <--- ЗБЕРІГАЄМО КОНФІГ В БД
            status='starting'
        )
        db.session.add(new_server)
        db.session.commit()

        # 2. Запускаємо Docker контейнер
        client = docker.from_env()
        try:
            docker_ports = {}
            assigned_ports_db = {}
            current_external_port = external_port

            for internal_port, protocol in template.default_ports.items():
                port_key = f"{internal_port}/{protocol}"
                docker_ports[port_key] = current_external_port
                assigned_ports_db[internal_port] = current_external_port
                current_external_port += 1

            new_server.assigned_ports = assigned_ports_db

            # --- ВИЗНАЧАЄМО ШЛЯХ ДЛЯ ЗБЕРЕЖЕННЯ СВІТУ ---
            bind_path = '/data' if 'Minecraft' in template.name else '/home/steam/Zomboid'
            volume_name = f"server_data_{new_server.uuid}"

            container = client.containers.run(
                image=template.docker_image,
                detach=True,
                name=f"server_{new_server.uuid}",
                ports=docker_ports,
                environment=new_server.env_vars,  # <--- Беремо конфіг з БД
                mem_limit=f"{form.ram.data}m",
                volumes={volume_name: {'bind': bind_path, 'mode': 'rw'}},  # <--- ДАНІ ТЕПЕР У БЕЗПЕЦІ
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
@login_required
@admin_required
def seed_db():
    games = [
        {
            'name': 'Minecraft Java',
            'docker_image': 'itzg/minecraft-server',
            'ports': {'25565': 'tcp'},
            'env': {
                'EULA': 'TRUE',
                'VERSION': 'LATEST',
                'ENABLE_RCON': 'true',
                'RCON_PASSWORD': current_app.config['RCON_PASSWORD'] # <--- current_app замість app
            }
        },
        {
            'name': 'Project Zomboid',
            'docker_image': 'renegademaster/zomboid-dedicated-server',
            # Вказуємо всі три порти, які потрібні грі
            'ports': {'16261': 'udp', '16262': 'udp', '8766': 'udp'},
            'env': {
                'ADMIN_PASSWORD': 'admin',
                'SERVER_NAME': 'MyZomboidServer',
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
            # --- ПЕРЕВІРКА НА АПРУВ ---
            if not user.is_approved:
                flash('Ваш акаунт очікує підтвердження адміністратором.', 'warning')
                return redirect(url_for('main.login'))

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

        # --- ЛОГІКА РОЛЕЙ ---
        # Якщо в базі ще немає користувачів, перший стає адміном
        is_first_user = User.query.count() == 0
        role = 'admin' if is_first_user else 'user'
        is_approved = True if is_first_user else False

        new_user = User(
            username=form.username.data,
            email=form.email.data,
            password_hash=hashed_password,
            role=role,
            is_approved=is_approved
        )

        try:
            db.session.add(new_user)
            db.session.commit()
            if is_first_user:
                flash('Акаунт Адміністратора створено! Можете увійти.', 'success')
            else:
                flash('Акаунт створено! Очікуйте на підтвердження адміністратором.', 'info')
            return redirect(url_for('main.login'))
        except:
            flash('Такий користувач або email вже існує.', 'danger')

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
    if server.owner_id != current_user.id and current_user.role != 'admin':
        abort(403)
    return render_template('server_details.html', server=server)


@bp.route('/server/<int:server_id>/<action>')
@login_required
def server_action(server_id, action):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
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

            # --- НОВИЙ ЕКШЕН ---
        elif action == 'rebuild':
            container.stop()
            container.remove()  # Видаляємо старий контейнер

            # Створюємо новий з новими налаштуваннями
            bind_path = '/data' if 'Minecraft' in server.template.name else '/home/steam/Zomboid'
            volume_name = f"server_data_{server.uuid}"
            docker_ports = {f"{k}/udp" if 'Zomboid' in server.template.name else f"{k}/tcp": v for k, v in
                            server.assigned_ports.items()}

            client.containers.run(
                image=server.template.docker_image,
                detach=True,
                name=container_name,
                ports=docker_ports,
                environment=server.env_vars,  # Беремо свіжі налаштування з БД
                mem_limit=f"{server.allocated_ram}m",
                volumes={volume_name: {'bind': bind_path, 'mode': 'rw'}},
                restart_policy={"Name": "on-failure"}
            )
            server.status = 'running'
            flash('Сервер перезібрано з новими налаштуваннями!', 'success')

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
    if server.owner_id != current_user.id and current_user.role != 'admin':
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
    if server.owner_id != current_user.id and current_user.role != 'admin':
        return jsonify({'error': 'Access denied'}), 403

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")
        # Отримуємо останні 100 рядків логів
        logs = container.logs(tail=100).decode('utf-8', errors='replace')
        return jsonify({'logs': logs, 'status': container.status})
    except Exception as e:
        return jsonify({'logs': f"Error fetching logs: {e}", 'status': 'unknown'})


@bp.route('/server/<int:server_id>/stats')
@login_required
def server_stats(server_id):
    """Отримує метрики CPU та RAM напряму від Docker API"""
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")

        # Якщо сервер не запущений, немає сенсу брати статистику
        if container.status != 'running':
            return jsonify({'status': 'error', 'message': 'Container is stopped'})

        # Отримуємо статистику (stream=False бере один "знімок" даних)
        stats = container.stats(stream=False)

        # --- РОЗРАХУНОК RAM ---
        mem_stats = stats.get('memory_stats', {})
        ram_usage = mem_stats.get('usage', 0)
        ram_mb = round(ram_usage / (1024 * 1024), 2)

        # --- РОЗРАХУНОК CPU (%) ---
        cpu_stats = stats.get('cpu_stats', {})
        precpu_stats = stats.get('precpu_stats', {})

        cpu_delta = cpu_stats.get('cpu_usage', {}).get('total_usage', 0) - precpu_stats.get('cpu_usage', {}).get(
            'total_usage', 0)
        system_delta = cpu_stats.get('system_cpu_usage', 0) - precpu_stats.get('system_cpu_usage', 0)

        cpu_percent = 0.0
        if system_delta > 0 and cpu_delta > 0:
            percpu_usage = cpu_stats.get('cpu_usage', {}).get('percpu_usage', [])
            num_cores = len(percpu_usage) if percpu_usage else os.cpu_count() or 1
            cpu_percent = (cpu_delta / system_delta) * num_cores * 100.0

        return jsonify({
            'status': 'success',
            'ram_mb': ram_mb,
            'cpu_percent': round(cpu_percent, 2)
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@bp.route('/server/<int:server_id>/download_logs')
@login_required
def download_logs(server_id):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        abort(403)

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")
        # Беремо всі логи з контейнера (не тільки останні 100)
        logs = container.logs().decode('utf-8', errors='replace')

        # Повертаємо текстовий файл "на льоту"
        return Response(
            logs,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename=server_{server.name}_logs.txt"}
        )
    except Exception as e:
        flash(f'Помилка завантаження логів: {e}', 'danger')
        return redirect(url_for('main.server_details', server_id=server.id))


@bp.route('/server/<int:server_id>/rcon', methods=['POST'])
@login_required
def send_rcon(server_id):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403

    command = request.json.get('command')
    if not command:
        return jsonify({'status': 'error', 'message': 'Empty command'})

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")

        # Отримуємо ВНУТРІШНІЙ IP контейнера у мережі Docker (щоб не йти через UFW)
        container.reload()
        networks = container.attrs['NetworkSettings']['Networks']
        ip_address = list(networks.values())[0]['IPAddress']

        # Визначаємо порт RCON залежно від гри
        template_name = server.template.name
        rcon_port = 25575 if 'Minecraft' in template_name else 27015
        rcon_pass = current_app.config['RCON_PASSWORD']

        try:
            # Спроба відправити через класичний протокол RCON
            with MCRcon(ip_address, rcon_pass, port=rcon_port) as mcr:
                response = mcr.command(command)
            return jsonify({'status': 'success', 'response': response})
        except Exception as rcon_e:
            # Надійний Fallback (якщо гра ще не підняла RCON порт)
            # Відправляємо команду напряму через Docker Exec (як у терміналі)
            if 'Minecraft' in template_name:
                exit_code, output = container.exec_run(f"rcon-cli {command}")
            else:
                exit_code, output = container.exec_run(f"rcon {command}")

            return jsonify({'status': 'success', 'response': output.decode('utf-8')})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@bp.route('/server/<int:server_id>/settings', methods=['POST'])
@login_required
def update_settings(server_id):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        abort(403)

    # Збираємо всі поля з форми, окрім CSRF токенів
    updated_env = {}
    for key, value in request.form.items():
        if key != 'csrf_token':
            updated_env[key] = value

    # Оновлюємо JSON у базі
    server.env_vars = updated_env
    db.session.commit()

    flash("Налаштування збережено! Щоб вони запрацювали, натисніть 'Rebuild' (Перезібрати).", "success")
    return redirect(url_for('main.server_details', server_id=server.id))


# --- РЕДАКТОР КОНФІГІВ ---

@bp.route('/server/<int:server_id>/config', methods=['GET', 'POST'])
@login_required
def server_config(server_id):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        abort(403)

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")
    except docker.errors.NotFound:
        flash("Контейнер не знайдено. Спочатку запустіть сервер.", "danger")
        return redirect(url_for('main.server_details', server_id=server.id))

    # Визначаємо шлях до конфігу залежно від гри
    if 'Minecraft' in server.template.name:
        config_path = '/data/server.properties'
    else:
        # Для Zomboid назва файлу залежить від SERVER_NAME
        server_name = server.env_vars.get('SERVER_NAME', 'servertest')
        config_path = f'/home/steam/Zomboid/Server/{server_name}.ini'

    # ОБРОБКА ЗБЕРЕЖЕННЯ
    if request.method == 'POST':
        new_content = request.form.get('config_content')
        if new_content is not None:
            # Кодуємо текст у Base64, щоб Linux не зламався від лапок чи спецсимволів
            encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')

            # Декодуємо і записуємо прямо всередині контейнера
            write_cmd = f"sh -c 'echo {encoded_content} | base64 -d > \"{config_path}\"'"
            exit_code, output = container.exec_run(write_cmd)

            if exit_code == 0:
                flash('Конфігурацію збережено! Натисніть Restart, щоб гра її підтягнула.', 'success')
            else:
                flash(f'Помилка збереження: {output.decode("utf-8")}', 'danger')

        return redirect(url_for('main.server_config', server_id=server.id))

    # GET ЗАПИТ: Читаємо поточний файл
    exit_code, output = container.exec_run(f"cat \"{config_path}\"")
    if exit_code == 0:
        config_content = output.decode('utf-8')
    else:
        config_content = f"# Файл {config_path} ще не створено.\n# Дочекайтеся повного завантаження сервера (генерації світу), а потім оновіть сторінку."

    return render_template('config_editor.html', server=server, config_content=config_content, config_path=config_path)


# --- ПАНЕЛЬ АДМІНІСТРАТОРА ---

@bp.route('/admin/users')
@login_required
@admin_required
def admin_users():
    users = User.query.all()
    return render_template('admin_users.html', users=users)


@bp.route('/admin/users/<int:user_id>/toggle_approve')
@login_required
@admin_required
def toggle_approve(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Ви не можете змінити статус самому собі.", "warning")
        return redirect(url_for('main.admin_users'))

    user.is_approved = not user.is_approved
    db.session.commit()
    status = "схвалено" if user.is_approved else "заблоковано"
    flash(f'Користувача {user.username} {status}.', 'success')
    return redirect(url_for('main.admin_users'))

@bp.route('/admin/servers')
@login_required
@admin_required
def admin_servers():
    # Беремо всі сервери з бази
    all_servers = GameServer.query.all()
    return render_template('admin_servers.html', servers=all_servers)



