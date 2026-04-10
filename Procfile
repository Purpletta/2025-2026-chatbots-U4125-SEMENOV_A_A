# Один воркер: иначе поднимется несколько экземпляров PTB. Порт только из $PORT (Railway Web).
web: gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT -t 120 bot:app
