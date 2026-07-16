from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, abort, Response, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
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
import io
import tarfile
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


# Папки Minecraft-сервера, доступні для завантаження .jar файлів.
# Жорсткий whitelist: користувач ніяк не може вказати іншу папку.
MC_JAR_FOLDERS = {
    'plugins': '/data/plugins',  # Paper / Spigot / Purpur
    'mods': '/data/mods',        # Forge / Fabric
}
MAX_JAR_SIZE = 50 * 1024 * 1024  # 50 MB на один .jar


def build_environment(server):
    """Env для контейнера: конфіг з БД + автоматичний розмір хіпа JVM для Minecraft.

    mem_limit обмежує лише контейнер — сам образ itzg без env MEMORY
    ставить JVM всього -Xmx1G. Тому якщо користувач не задав памʼять явно,
    даємо хіпу 75% від виділеного RAM (решта — на off-heap: netty, metaspace).
    """
    env = dict(server.env_vars or {})
    if 'Minecraft' in server.template.name and not any(
            k in env for k in ('MEMORY', 'MAX_MEMORY', 'INIT_MEMORY')):
        env['MEMORY'] = f"{int(server.allocated_ram * 0.75)}M"
    return env


def list_jars(container, folder):
    """Повертає відсортований список .jar файлів у папці контейнера.

    Якщо контейнер запущений — через exec_run (швидко).
    Якщо зупинений — через get_archive (Docker API дозволяє читати
    файлову систему навіть зупиненого контейнера).
    """
    path = MC_JAR_FOLDERS[folder]
    try:
        if container.status == 'running':
            exit_code, output = container.exec_run(["ls", "-1", path])
            if exit_code != 0:
                return []
            names = output.decode('utf-8', errors='replace').splitlines()
        else:
            stream, _ = container.get_archive(path)
            buf = io.BytesIO(b''.join(stream))
            with tarfile.open(fileobj=buf) as tar:
                names = [os.path.basename(m.name) for m in tar.getmembers() if m.isfile()]
        return sorted(n for n in names if n.lower().endswith('.jar'))
    except Exception:
        # Папки ще немає (сервер жодного разу не стартував з Paper) — це не помилка
        return []


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
                environment=build_environment(new_server),  # конфіг з БД + авто-Xmx
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

    # Для Minecraft показуємо вміст plugins/ та mods/
    is_minecraft = 'Minecraft' in server.template.name
    jar_folders = {}
    if is_minecraft:
        try:
            container = docker.from_env().containers.get(f"server_{server.uuid}")
            jar_folders = {name: list_jars(container, name) for name in MC_JAR_FOLDERS}
        except docker.errors.NotFound:
            jar_folders = {name: [] for name in MC_JAR_FOLDERS}

    return render_template('server_details.html', server=server,
                           is_minecraft=is_minecraft, jar_folders=jar_folders)


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
                environment=build_environment(server),  # свіжі налаштування з БД + авто-Xmx
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


# --- ПЛАГІНИ ТА МОДИ (Minecraft) ---

@bp.route('/server/<int:server_id>/plugins/upload', methods=['POST'])
@login_required
def upload_plugin(server_id):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        abort(403)
    if 'Minecraft' not in server.template.name:
        abort(400)

    # 1. Валідація папки призначення (тільки whitelist)
    folder = request.form.get('folder', 'plugins')
    if folder not in MC_JAR_FOLDERS:
        abort(400)

    # 2. Валідація файлу
    file = request.files.get('file')
    if not file or not file.filename:
        flash('Файл не вибрано.', 'warning')
        return redirect(url_for('main.server_details', server_id=server.id))

    # secure_filename прибирає '../', слеші та інші небезпечні символи
    filename = secure_filename(file.filename)
    if not filename or not filename.lower().endswith('.jar'):
        flash('Дозволені тільки файли з розширенням .jar.', 'danger')
        return redirect(url_for('main.server_details', server_id=server.id))

    data = file.read()
    if len(data) > MAX_JAR_SIZE:
        flash(f'Файл завеликий. Максимум — {MAX_JAR_SIZE // (1024 * 1024)} MB.', 'danger')
        return redirect(url_for('main.server_details', server_id=server.id))
    # .jar — це zip-архів, він завжди починається з сигнатури "PK"
    if not data.startswith(b'PK'):
        flash('Файл не схожий на справжній .jar (zip) архів.', 'danger')
        return redirect(url_for('main.server_details', server_id=server.id))

    # 3. Кладемо файл у контейнер через Docker API (працює навіть якщо він зупинений)
    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")

        # Пакуємо jar у tar в пам'яті: put_archive приймає тільки tar-архіви
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tar:
            dir_info = tarfile.TarInfo(name=folder)
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755
            dir_info.uid = dir_info.gid = 1000  # користувач minecraft в образі itzg
            tar.addfile(dir_info)

            info = tarfile.TarInfo(name=f"{folder}/{filename}")
            info.size = len(data)
            info.mode = 0o644
            info.uid = info.gid = 1000
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        container.put_archive('/data', buf)

        if request.form.get('restart') and container.status == 'running':
            container.restart()
            flash(f'"{filename}" завантажено у {folder}/. Сервер перезапускається...', 'success')
        else:
            flash(f'"{filename}" завантажено у {folder}/. Натисніть Restart, щоб сервер його підхопив.', 'success')

    except docker.errors.NotFound:
        flash('Контейнер не знайдено! Можливо, він був видалений вручну.', 'danger')
    except Exception as e:
        flash(f'Помилка завантаження: {e}', 'danger')

    return redirect(url_for('main.server_details', server_id=server.id))


@bp.route('/server/<int:server_id>/plugins/delete', methods=['POST'])
@login_required
def delete_plugin(server_id):
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        abort(403)

    folder = request.form.get('folder', 'plugins')
    if folder not in MC_JAR_FOLDERS:
        abort(400)

    # Та сама санітизація, що і при завантаженні — видалити можна тільки .jar у whitelist-папці
    filename = secure_filename(request.form.get('filename', ''))
    if not filename or not filename.lower().endswith('.jar'):
        abort(400)

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")
        if container.status != 'running':
            flash('Видалення файлів працює тільки на запущеному сервері. Натисніть Start.', 'warning')
            return redirect(url_for('main.server_details', server_id=server.id))

        # Список аргументів (без shell) — ін'єкція команд неможлива
        exit_code, output = container.exec_run(["rm", f"{MC_JAR_FOLDERS[folder]}/{filename}"])
        if exit_code == 0:
            flash(f'"{filename}" видалено. Натисніть Restart, щоб зміни застосувались.', 'success')
        else:
            flash(f'Не вдалося видалити: {output.decode("utf-8", errors="replace")}', 'danger')

    except docker.errors.NotFound:
        flash('Контейнер не знайдено!', 'danger')
    except Exception as e:
        flash(f'Помилка видалення: {e}', 'danger')

    return redirect(url_for('main.server_details', server_id=server.id))


@bp.route('/server/<int:server_id>/download_world')
@login_required
def download_world(server_id):
    """Скачує папку світу як .tar архів (бекап). Працює і на зупиненому сервері."""
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        abort(403)
    if 'Minecraft' not in server.template.name:
        abort(400)

    client = docker.from_env()
    try:
        container = client.containers.get(f"server_{server.uuid}")
        # get_archive повертає потік tar-архіву — віддаємо його напряму, без буферизації в RAM
        stream, _ = container.get_archive('/data/world')
        return Response(
            stream,
            mimetype='application/x-tar',
            headers={"Content-disposition": f"attachment; filename=world_{server.uuid}.tar"}
        )
    except docker.errors.NotFound:
        flash('Світ ще не створено або контейнер не знайдено.', 'warning')
    except Exception as e:
        flash(f'Помилка завантаження світу: {e}', 'danger')
    return redirect(url_for('main.server_details', server_id=server.id))


@bp.route('/server/<int:server_id>/enable_paper', methods=['POST'])
@login_required
def enable_paper(server_id):
    """Перемикає ядро Minecraft на Paper (підтримка плагінів). Світ зберігається."""
    server = GameServer.query.get_or_404(server_id)
    if server.owner_id != current_user.id and current_user.role != 'admin':
        abort(403)
    if 'Minecraft' not in server.template.name:
        abort(400)

    # JSONB не відстежує зміни всередині dict — тому створюємо новий
    server.env_vars = {**(server.env_vars or {}), 'TYPE': 'PAPER'}
    db.session.commit()
    flash("TYPE=PAPER збережено. Натисніть 'Rebuild' — сервер перезбереться з ядром Paper, світ залишиться.", 'success')
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


