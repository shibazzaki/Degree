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
    # Заповнюються JS-пошуком модпаків на сторінці створення (тільки Minecraft)
    modpack_platform = HiddenField()
    modpack_slug = HiddenField()
    submit = SubmitField('Створити сервер')