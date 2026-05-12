# Используем стабильный Python
FROM python:3.11

# Создаем рабочую папку
WORKDIR /code

# Копируем список библиотек и устанавливаем их
COPY ./requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Копируем весь код
COPY . .

# Запускаем бота (файл должен называться app.py)
CMD ["python", "app.py"]
