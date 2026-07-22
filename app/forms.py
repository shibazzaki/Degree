from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField, SelectField, IntegerField, HiddenField
from wtforms.validators import DataRequired, Email, EqualTo, Length

class LoginForm(FlaskForm):
    username = StringField('Логін', validators=[DataRequired()])
    password = PasswordField('Пароль', validators=[DataRequired()])
    submit = SubmitField('Увійти')

class RegistrationForm(FlaskForm):
    username = StringField('Логін', validators=[DataRequired(), Length(min=4, max=20)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Пароль', validators=[DataRequired()])
    confirm_password = PasswordField('Повторіть пароль', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Зареєструватися')

class CreateServerForm(FlaskForm):
    name = StringField('Назва сервера', validators=[DataRequired(), Length(min=3, max=20)])
    game_id = SelectField('Оберіть гру', coerce=int, validators=[DataRequired()])
    ram = IntegerField('RAM (MB)', default=1024, validators=[DataRequired()])
    # Тільки для Minecraft — env DIFFICULTY образу itzg
    difficulty = SelectField('Складність', default='normal', choices=[
        ('peaceful', 'Peaceful (мирна)'),
        ('easy', 'Easy (легка)'),
        ('normal', 'Normal (звичайна)'),
        ('hard', 'Hard (складна)'),
    ])
    # Версія Java (тільки Minecraft) — тег образу itzg. Старі паки (1.12.2) = Java 8
    java = SelectField('Версія Java', default='auto', choices=[
        ('auto', 'Авто (за версією гри)'),
        ('java8', 'Java 8 (старі паки: 1.12.2 і старіші, напр. SkyFactory 4)'),
        ('java17', 'Java 17 (1.17–1.20)'),
        ('java21', 'Java 21 (1.21+)'),
    ])
    # Заповнюються JS-пошуком модпаків на сторінці створення (тільки Minecraft)
    modpack_platform = HiddenField()
    modpack_slug = HiddenField()
    submit = SubmitField('Створити сервер')