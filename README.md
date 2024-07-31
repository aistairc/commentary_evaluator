# Video Evaluation

**Warning**: This is an early alpha version; use at your own risk.

## Prerequisites

FFmpeg is required; you can use your system's package manager, or through sources: https://www.ffmpeg.org/download.html

## Installation

```
python3.10 -m venv v-video-evaluation
source v-video-evaluation/bin/activate
pip install --upgrade pip
pip install -r human-requirements.txt
```

## Configuration

Create `video_evaluation/local_settings.py`:

```
SECRET_KEY = 'some long and unguessable string, typically 60+ random printable ASCII characters'

# For production:
# DEBUG = False
# ALLOWED_HOSTS = ['127.0.0.1'] # see https://docs.djangoproject.com/en/5.0/ref/settings/#allowed-hosts
# DATABASES = { ... } # see https://docs.djangoproject.com/en/5.0/ref/settings/#databases
# MEDIA_ROOT = '...'
# STATIC_ROOT = '...'

# _MB = 1024 * 1024
# FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * _MB
```

Edit `Procfile` to choose the port

## Finishing setup

```
python manage.py migrate
python manage.py createsuperuser
```

If running in production mode,

```
python manage.py collectstatic
```

and then set up reverse proxy for the app, and make sure `STATIC_ROOT` and `MEDIA_ROOT` are served by the web server.

## Running (development mode)

```
honcho start
```

## Terms

- Dataset: collection of Videos+audios+subtitles and cut definition JSON - cut into Segments
- Project: an evaulation Project, with question definitions; collection of Tasks
- Task: an evaluation task on a single Segment for a specific evaluation Project
- Assignment: answers to the defined questions for a specific Task

## Flow

- Admin creates users (plan for later: Project managers will invite users)
- Admin assigns "Video Eval App | dataset | can add dataset" permission to trusted users
- If those users create a Dataset, they will become a Dataset Manager for that Dataset
- Dataset managers can upload videos (+audio, +subtitles) into a Dataset
- They can add other users as Dataset Managers. They can also create projects in that Dataset, and thus become Project Managers for those Projects
- Project Managers can appoint additional Project Managers, as well as Project Evaluators
- Project Evaluators can work on Tasks, answering Project-defined questions on each Segment of the Dataset
- Project Managers can at any time download the results of the evaluation in their Project

## Caveats (early alpha version)

* Users must be created by superuser. Later I intend to integrate an email invitation system, available to the Project Managers.
- A user that will create a dataset must be manually given the permission `Video Eval App | dataset | can add dataset` in the Admin interface (`http://localhost:3000/admin` -> Users -> [user] -> User permissions (Admin can also create a dataset, they don't need a permission)
- I have not worked on deletion yet; anything deleted from the Admin interface may leave behind artefacts, and orphaned files in `MEDIA_ROOT`
- User interface could be made much prettier
- There are very likely other bugs, and many unpoplished edges
