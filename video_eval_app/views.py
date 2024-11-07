from icecream import ic # XXX: delete later

from django.contrib.admin.options import messages

import json
from datetime import datetime
from io import BytesIO
import os

from django.core.files import File
from django.conf import settings
from django.shortcuts import HttpResponseRedirect, render, redirect
from django.http import HttpResponse, JsonResponse, Http404
from django.core.paginator import Paginator
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_safe
from django.db.utils import IntegrityError
from django.db.models import Exists, OuterRef
from django.utils.safestring import SafeString
from django.contrib.auth import login
from guardian.shortcuts import assign_perm, remove_perm, get_objects_for_user, get_users_with_perms
from invitations.utils import get_invitation_model
import botocore
import nh3

from video_evaluation.settings import CREDENTIALS_COOKIE_NAME, MTURK_SANDBOX

from .models import *
from .mturk import MTurk, make_aws_session
from .utils import convert_answers, load_subtitles
from .storage import StoredFile


Invitation = get_invitation_model()


ITEMS_PER_PAGE = 10




def get_request_credentials(request):
    if file_credentials := request.FILES.get('credentials'):
        raw_credentials = file_credentials.read().decode()
    else:
        raw_credentials = request.POST.get('credentials')
    if raw_credentials:
        try:
            credentials = json.loads(raw_credentials)
        except json.decoder.JSONDecodeError:
            raise ValueError("AWS credentials are not a valid JSON")
    else:
        return None
    if 'Credentials' in credentials:
        credentials = credentials['Credentials']

    location = request.POST.get('location')
    if location:
        credentials['Location'] = location
    else:
        location = credentials.get('Location')
    if location:
        bucket = location.split('/', 1)[0]
        session = make_aws_session(credentials)
        s3 = session.client('s3')
        try:
            cors = s3.get_bucket_cors(Bucket=bucket)
        except botocore.exceptions.ClientError:
            cors = {
                "CORSRules": [],
            }
        cors_rules = cors["CORSRules"]
        changed = False
        for item in settings.S3_CORS_RULES:
            if item not in cors_rules:
                cors_rules.append(item)
                changed = True
        if changed:
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors)

    return credentials


def upload_video(request, dataset, credentials):
    location = credentials and credentials.pop('Location')
    session = credentials and location and make_aws_session(credentials)

    video_file = StoredFile.save(request.FILES["file"], "video_files", session, location, with_md5=True)
    audio = StoredFile.save(request.FILES.get("audio"), "audio_files", session, location)
    if raw_subtitles_file := request.FILES.get('subtitles'):
        with raw_subtitles_file.open('rb') as r:
            raw_subs = r.read()
        subs = load_subtitles(sub_contents=raw_subs)
        subs_base, _ = os.path.splitext(raw_subtitles_file.name)
        subs_name = f"{subs_base}.vtt"
        subs_file = File(file=BytesIO(subs.content.encode()), name=subs_name)
    else:
        subs_file = None
    subtitles = StoredFile.save(subs_file, "sub_files", session, location)
    cuts = request.FILES.get('cuts')
    if cuts:
        with cuts.open('rt') as r:
            cuts_data = json.load(r)
    else:
        cuts_data = None

    video_md5sum = video_file.pop('md5')
    video, _ = Video.objects.get_or_create(pk=video_md5sum, defaults = {
        "file": video_file,
        # "created_by": request.user, # XXX: move token to UserProfile?
    })
    dataset_video = DatasetVideo.objects.create(
        dataset=dataset,
        video=video,
        subtitles=subtitles,
        audio=audio,
        name=request.POST.get('name', ''),
        cuts=cuts_data,
    )
    async_task(
        'video_eval_app.tasks.cut_and_delocalize_video',
        dataset_video, credentials, location,
    )

# ---

@require_safe
def index(request):
    return render(request, 'index.html')

@login_required
@require_safe
def datasets(request):
    datasets = (
            # managed datasets or
            get_objects_for_user(request.user, 'video_eval_app.manage_dataset') |
            # datasets of managed projects
            Dataset.objects.filter(
                projects__in=get_objects_for_user(request.user, 'video_eval_app.manage_project')
            )
    ).distinct().order_by('name')
    paginator = Paginator(datasets, ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    add_dataset_perm = request.user.has_perm('video_eval_app.add_dataset')
    new_url = add_dataset_perm and request.user.is_authenticated and reverse('datasets_new')
    return render(request, 'datasets.html', {
        'page': page,
        'new_url': add_dataset_perm and new_url,
    })

@login_required
def datasets_new(request):
    if not request.user.has_perm('video_eval_app.add_dataset'):
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        return render(request, 'datasets_new.html')
    elif request.method == 'POST':
        name = request.POST['name']
        dataset = Dataset.objects.create(
            name=name,
            created_by=request.user,
        )
        assign_perm('manage_dataset', request.user, dataset)
        return redirect('dataset_videos', dataset.id)

@login_required
def dataset_edit(request, dataset_id):
    dataset = Dataset.objects.get(pk=dataset_id)
    manage_dataset_perm = request.user.has_perm('manage_dataset', dataset)
    if not manage_dataset_perm:
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        upload_video_url = request.user.is_authenticated and request.build_absolute_uri(reverse('upload_video_api', args=[dataset.token]))
        return render(request, 'dataset_edit.html', {
            "editable": manage_dataset_perm,
            "dataset": dataset,
            "upload_video_url": manage_dataset_perm and upload_video_url,
        })
    elif request.method == 'POST':
        dataset.name = request.POST['name']
        if request.POST['renew'] == '1':
            dataset.renew_token()
        dataset.save()
        return redirect(request.path_info)

@csrf_exempt
@require_POST
def upload_video_api(request, token):
    try:
        credentials = get_request_credentials(request)
        dataset = Dataset.objects.get(token=token)
        upload_video(request, dataset, credentials)
        return HttpResponse(status=204)
    except Dataset.DoesNotExist:
        return HttpResponse("Invalid token.\n", status=403)
    except IntegrityError:
        return HttpResponse("This video is already in this dataset.\n", status=400)

@login_required
@require_safe
def dataset_videos(request, dataset_id):
    dataset = Dataset.objects.get(pk=dataset_id)
    manage_dataset_perm = request.user.has_perm('manage_dataset', dataset)
    has_managed_projects = Dataset.objects.filter(
        id=dataset.id,
        projects__in=get_objects_for_user(request.user, 'video_eval_app.manage_project')
    ).distinct()
    if not (manage_dataset_perm or has_managed_projects):
        return HttpResponse('Forbidden', status=403)
    paginator = Paginator(dataset.dataset_videos.order_by('name').all(), ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    new_url = request.user.is_authenticated and reverse('dataset_videos_new', args=[dataset.id])
    return render(request, 'dataset_videos.html', {
        "dataset": dataset,
        "page": page,
        "new_url": new_url,
    })

@login_required
def dataset_video(request, dataset_id, dataset_video_id=None):
    dataset = Dataset.objects.get(pk=dataset_id)
    manage_dataset_perm = request.user.has_perm('manage_dataset', dataset)
    has_managed_projects = Dataset.objects.filter(
        id=dataset.id,
        projects__in=get_objects_for_user(request.user, 'video_eval_app.manage_project')
    ).distinct()
    
    if not (manage_dataset_perm or has_managed_projects):
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        if dataset_video_id:
            dataset_video = DatasetVideo.objects.get(pk=dataset_video_id, dataset=dataset)
            paginator = Paginator(dataset_video.segments.order_by('start').all(), ITEMS_PER_PAGE)
            page_number = request.GET.get("page")
            page = paginator.get_page(page_number)
        else:
            dataset_video = DatasetVideo(dataset=dataset)
            page = None
        return render(request, 'dataset_video.html', {
            'editable': manage_dataset_perm,
            'dataset': dataset,
            'dataset_video': dataset_video,
            'page': page,
        })
    elif request.method == 'POST':
        upload_video(request, dataset, request.credentials)
        return redirect('dataset_videos', dataset_id=dataset.id)

@login_required
@require_safe
def dataset_projects(request, dataset_id):
    dataset = Dataset.objects.get(pk=dataset_id)
    manage_dataset_perm = request.user.has_perm('manage_dataset', dataset)
    if manage_dataset_perm:
        projects = dataset.projects.order_by('name').all()
    else:
        projects = get_objects_for_user(request.user, 'video_eval_app.manage_project').filter(dataset=dataset).order_by('name')
    paginator = Paginator(projects, ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    new_url = manage_dataset_perm and reverse('dataset_project_new', args=[dataset.id])
    return render(request, 'dataset_projects.html', {
        "dataset": dataset,
        "page": page,
        "new_url": new_url,
    })

@login_required
def dataset_project(request, dataset_id, project_id=None):
    dataset = Dataset.objects.get(pk=dataset_id)
    if project_id:
        project = Project.objects.get(pk=project_id)
        if not request.user.has_perm('video_eval_app.manage_project', project):
            return HttpResponse('Forbidden', status=403)
    else:
        if not request.user.has_perm('manage_dataset', dataset):
            return HttpResponse('Forbidden', status=403)
        project = Project()
    if request.method in {"GET", "HEAD"}:
        if project_id:
            num_approved_assignments = Assignment.objects.filter(
                task__project=project, is_approved=True,
            ).count()
        else:
            num_approved_assignments = None
        return render(request, 'dataset_project.html', {
            'dataset': dataset,
            'project': project,
            'project_messages': project.messages,
            'questions': SafeString(json.dumps(project.questions, ensure_ascii=False)),
            'turk_settings': SafeString(json.dumps(project.turk_settings, ensure_ascii=False)),
            'num_uncut_videos': dataset.dataset_videos.filter(is_cut=False).count(), # TODO: disable "Start" button if >0, show an info message
            'preview_url': MTurk.get_environment()['preview'] + '?groupId=' + project.turk_hit_group_id if project.turk_hit_group_id else None,
            'busy_disabled': 'disabled' if project.is_busy else '',
            'cred_busy_disabled': 'disabled' if project.is_busy or not request.credentials else '',
            'num_approved_assignments': num_approved_assignments,
        })
    elif request.method == 'POST':
        if project.is_busy:
            messages.warning('The project became busy, the operation was not performed. Please try again later.')
            pass # do nothing, redirect back
        elif request.POST.get('dismiss_messages'):
            project.messages = []
            project.save()
        elif request.POST.get('collect_mturk'):
            async_task(
                'video_eval_app.tasks.get_assignments_from_mturk',
                project, request.credentials,
            )
        else:
            num_uncut_videos = dataset.dataset_videos.filter(is_cut=False).count()
            if num_uncut_videos:
                message.warning(f'{num_uncut_videos} video(s) still being processed')
                return redirect('dataset_project', dataset_id=dataset.id, project_id=project.id)
            turk_settings = request.POST["turk_settings"]
            turk_settings = json.loads(request.POST["turk_settings"])
            questions = json.loads(request.POST["questions"])
            for question in questions:
                question["instruction"] = nh3.clean(question["instruction"])
                if options := question["options"]:
                    for option in options:
                        option["text"] = nh3.clean(option["text"])
            defaults = {
                "name": request.POST["name"],
                "questions": questions,
                "turk_settings": turk_settings,
            }
            will_run_async_task = bool(turk_settings)
            create_defaults={
                **defaults,
                "created_by": request.user,
                "dataset": dataset,
                "is_busy": will_run_async_task,
            }
            project, created = Project.objects.update_or_create(
                id=int(request.POST["id"]),
                defaults=defaults,
                create_defaults=create_defaults,
            )
            if created:
                assign_perm('manage_project', request.user, project)
            if request.POST.get('start'):
                if turk_settings:
                    # get MTurk client and check if it works
                    mturk = MTurk()
                    mturk.connect(request.credentials)
                    mturk.get_account_balance()

                project.is_started = True
                project.save()
                segments = Segment.objects.filter(dataset_video__dataset=project.dataset)
                tasks = [
                    Task(project=project, segment=segment)
                    for segment in segments
                ]
                Task.objects.bulk_create(tasks)

                if turk_settings:
                    async_task(
                        'video_eval_app.tasks.post_project_to_mturk',
                        project, request.credentials,
                    )
        return redirect('dataset_project', dataset_id=dataset.id, project_id=project.id)

@login_required
@require_safe
def projects(request):
    manage_project_ids = set(
        get_objects_for_user(request.user, 'video_eval_app.manage_project')
            .values_list('id', flat=True)
    )
    evaluate_project_ids = set(
        get_objects_for_user(request.user, 'video_eval_app.evaluate_project')
            .values_list('id', flat=True)
    )
    manage_dataset_ids = set(
        get_objects_for_user(request.user, 'video_eval_app.manage_dataset')
            .values_list('id', flat=True)
    )
    projects = (
        Project.objects.filter(pk__in=(evaluate_project_ids | manage_project_ids))
            .prefetch_related('dataset').order_by('name')
    )
    paginator = Paginator(projects, ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    page.object_list = [
        {
            "project": project,
            "evaluate": True,
            "manage_project_perm": project.id in manage_project_ids,
            "evaluate_project_perm": project.id in evaluate_project_ids,
            "manage_dataset_perm": project.dataset.id in manage_dataset_ids,
        }
        for project in page.object_list
    ]
    return render(request, 'projects.html', { 'page': page })

@login_required
def dataset_managers(request, dataset_id):
    dataset = Dataset.objects.get(pk=dataset_id)
    if not request.user.has_perm('manage_dataset', dataset):
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        users = User.objects.filter(is_superuser=False).exclude(username='AnonymousUser').order_by('username')
        perms = get_users_with_perms(dataset, attach_perms=True)
        paginator = Paginator(users, ITEMS_PER_PAGE)
        page_number = request.GET.get("page")
        page = paginator.get_page(page_number)
        page.object_list = [
            (user, 'manage_dataset' in user_perms)
            for user, user_perms in (
                (user, perms.get(user, []))
                for user in page.object_list
            )
        ]
        new_url = request.user.has_perm('add_user') and reverse('dataset_invite', args=[dataset.id])
        return render(request, 'dataset_managers.html', {
            'dataset': dataset,
            'page': page,
            'new_url': new_url,
        })
    elif request.method == 'POST':
        user_id = request.POST["user_id"]
        manage = request.POST.get("manage")
        user = User.objects.get(pk=user_id)
        if manage:
            if manage == 'False':
                assign_perm('manage_dataset', user, dataset)
            else:
                remove_perm('manage_dataset', user, dataset)
        return redirect('dataset_managers', dataset_id=dataset.id)

@login_required
def project_approvals(request, project_id):
    project = Project.objects.get(pk=project_id)
    assignments = Assignment.objects.filter(task__project=project).order_by('task__segment__dataset_video__name', 'task__segment__start')
    paginator = Paginator(assignments, ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    return render(request, 'project_approvals.html', {
        'dataset': project.dataset,
        'project': project,
        'page': page,
    })

@login_required
def assignment(request, assignment_id):
    assignment = Assignment.objects.filter(pk=assignment_id).prefetch_related('task__project__dataset').first()
    if request.method == 'POST':
        feedback = request.POST.get('feedback')
        feedback_opt = {}
        if feedback:
            feedback_opt['RequesterFeedback'] = feedback
        if 'approve' in request.POST:
            if assignment.turk_assignment_id:
                mturk = MTurk()
                mturk.connect(request.credentials)
                response = mturk.client.approve_assignment(
                    AssignmentId=assignment.turk_assignment_id,
                    **feedback_opt,
                    OverrideRejection=True,
                )
            assignment.is_approved = True
            assignment.save()
        elif 'reject' in request.POST:
            if assignment.turk_assignment_id:
                if not feedback:
                    messages.error(request, 'MTurk rejection requires feedback')
                    return HttpResponseRedirect(request.path_info)
                if assignment.is_approved:
                    messages.error(request, 'Cannot reject an already approved MTurk assignment')
                    return HttpResponseRedirect(request.path_info)
                mturk = MTurk()
                mturk.connect(request.credentials)
                response = mturk.client.reject_assignment(
                    AssignmentId=assignment.turk_assignment_id,
                    **feedback_opt,
                )
            assignment.is_approved = False
            assignment.save()
        return redirect('project_approvals', project_id=assignment.task.project.id)
    else:
        return render(request, 'assignment.html', {
            'dataset': assignment.task.project.dataset,
            'project': assignment.task.project,
            'task': assignment.task,
            'assignment': assignment,
        })

@login_required
def project_users(request, project_id):
    project = Project.objects.get(pk=project_id)
    if not request.user.has_perm('manage_project', project):
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        users = User.objects.filter(is_superuser=False).exclude(username='AnonymousUser').order_by('username')
        perms = get_users_with_perms(project, attach_perms=True)
        paginator = Paginator(users, ITEMS_PER_PAGE)
        page_number = request.GET.get("page")
        page = paginator.get_page(page_number)
        page.object_list = [
            (user, 'evaluate_project' in user_perms, 'manage_project' in user_perms)
            for user, user_perms in (
                (user, perms.get(user, []))
                for user in page.object_list
            )
        ]
        new_url = request.user.has_perm('add_user') and reverse('project_invite', args=[project.id])
        return render(request, 'project_users.html', {
            'dataset': project.dataset,
            'project': project,
            'page': page,
            'new_url': new_url,
        })
    elif request.method == 'POST':
        user_id = request.POST["user_id"]
        manage = request.POST.get("manage")
        evaluate = request.POST.get("evaluate")
        user = User.objects.get(pk=user_id)
        if evaluate:
            if evaluate == 'False':
                assign_perm('evaluate_project', user, project)
            else:
                remove_perm('evaluate_project', user, project)
        if manage:
            if manage == 'False':
                assign_perm('manage_project', user, project)
            else:
                remove_perm('manage_project', user, project)
        return redirect('project_users', project_id=project.id)

@login_required
def invite_user(request, dataset_id=None, project_id=None):
    if project_id:
        project = Project.objects.get(pk=project_id)
        dataset = Dataset.objects.get(pk=project.dataset.id)
        if not request.user.has_perm('manage_project', project):
            return HttpResponse('Forbidden', status=403)
    elif dataset_id:
        dataset = Dataset.objects.get(pk=dataset_id)
        project = None
        if not request.user.has_perm('manage_dataset', dataset):
            return HttpResponse('Forbidden', status=403)
    if request.method in {'GET', 'HEAD'}:
        return render(request, 'invite_user.html', {
            'dataset': dataset,
            'project': project,
        })
    elif request.method == 'POST':
        role = request.POST['role']
        email = request.POST['email']
        invitation = Invitation.objects.filter(email__iexact=email).last()
        if invitation:
            pass # TODO: refuse to send duplicate invitation
        invitation = Invitation.create(email=email, role=role, dataset=dataset, project=project)
        invitation.send_invitation(request)
        if project:
            return redirect('project_users', project_id=project.id)
        else:
            return redirect('dataset_managers', dataset_id=dataset.id)

@login_required
def segment(request, segment_id):
    segment = Segment.objects.get(pk=segment_id)
    dataset = segment.dataset_video.dataset
    manage_dataset_perm = request.user.has_perm('manage_dataset', dataset)
    has_managed_projects = Dataset.objects.filter(
        id=dataset.id,
        projects__in=get_objects_for_user(request.user, 'video_eval_app.manage_project')
    ).distinct()
    if not (manage_dataset_perm or has_managed_projects): # TODO: or a project
        return HttpResponse('Forbidden', status=403)
    return render(request, 'segment.html', {
        'segment': segment,
        'dataset': dataset,
    })

@login_required
@require_safe
def project_eval(request, project_id):
    project = Project.objects.get(pk=project_id)
    evaluate_project_perm = request.user.has_perm('evaluate_project', project)
    if not evaluate_project_perm:
        return HttpResponse('Forbidden', status=403)
    worker, _created = Worker.objects.get_or_create(user=request.user)
    task = Task.objects.filter(
        ~Exists(Assignment.objects.filter(
            task_id=OuterRef('pk'),
            worker=worker,
        )),
        project=project,
    ).first()
    return render(request, 'project_eval.html', {
        'dataset': project.dataset,
        'project': project,
        'evaluate': True,
        'task': task,
        'dataset_video': task and task.segment.dataset_video,
    })

@require_POST
def task_eval_submit(request, task_id):
    task = Task.objects.get(pk=task_id)
    result = convert_answers(task.project.questions, request=request)
    Assignment.objects.create(
        task=task,
        worker=request.user.worker,
        result=result,
    )
    return redirect('project_eval', project_id=task.project.id)

@login_required
@require_safe
def project_results(request, project_id):
    project = Project.objects.get(pk=project_id)
    if not request.user.has_perm('video_eval_app.manage_project', project):
        return HttpResponse('Forbidden', status=403)
    tasks = (
        Task.objects.filter(project_id=project.id)
            .select_related('segment')
            .prefetch_related('segment__dataset_video')
    )
    assignments = Assignment.objects.filter(
        task__project_id=project_id, is_approved=True,
    )
    results = {
        "project": project.name,
        "tasks": {
            task.id: {
                "name": task.segment.dataset_video.name,
                "start": task.segment.start,
                "end": task.segment.end,
                "evaluations": [],
            }
            for task in tasks
        },
    }
    for assignment in assignments:
        results["tasks"][assignment.task_id]["evaluations"].append(assignment.result)
    results["tasks"] = list(results["tasks"].values())
    return HttpResponse(
        json.dumps(results, ensure_ascii=False),
        content_type="application/json"
    )

def accept_invite(request, key):
    try:
        invitation = Invitation.objects.get(key=key.lower())
    except Invitation.DoesNotExist:
        messages.error(request, "This invitation does not exist.")
        return redirect('index')
    if invitation.accepted:
        messages.error(request, "This invitation has been already accepted.")
        return redirect('index')
    if invitation.key_expired():
        messages.error(request, "This invitation has expired.")
        return redirect('index')

    if request.method in {"GET", "HEAD"}:
        return render(request, 'invite_signup.html', {
            'invitation': invitation,
            'username': request.POST.get('username', ''),
        })
    elif request.method == 'POST':
        password = request.POST['password1']
        if password != request.POST['password2']:
            messages.error(request, "The passwords do not match.")
            return HttpResponseRedirect(request.path_info)

        user = User.objects.create_user(
            username=request.POST['username'],
            email=invitation.email,
            password=password,
        )
        scope = invitation.project or invitation.dataset
        assign_perm(invitation.role, user, scope)

        login(request, user, backend='django.contrib.auth.backends.ModelBackend')

        invitation.accepted = True
        invitation.save()

        return redirect('index')

@login_required
def credentials(request):
    if request.method in {"GET", "HEAD"}:
        location = ''
        if request.credentials:
            location = request.credentials.pop('Location', '')
        credentials_text = SafeString(json.dumps(request.credentials, indent=4)) if request.credentials else ''
        return render(request, 'credentials.html', {
            "credentials": credentials_text,
            "location": location,
        })
    elif request.method == 'POST':
        try:
            credentials = get_request_credentials(request)
        except ValueError:
            messages.error(request, 'The credentials are not a valid JSON')
            return redirect(request.path_info)

        try:
            mturk = MTurk()
            mturk.connect(credentials)
            mturk.get_account_balance()
        except botocore.exceptions.ClientError:
            messages.error(request, 'AWS could not use the credentials')
            return redirect(request.path_info)

        location = credentials.get("Location")
        if location:
            session = make_aws_session(credentials)
            s3 = session.client('s3')
            key = 'video_eval_test_file.txt'
            body = b'Test'
            bucket, path = location.split('/', 1)
            if path:
                key = f"{path}/{key}"
            try:
                s3.put_object(Bucket=bucket, Key=key, Body=body, ACL='public-read')
            except botocore.exceptions.ClientError:
                messages.error(request, 'AWS S3 bucket does not exist or cannot be uploaded to')
                return redirect(request.path_info)

        expiration = credentials.get('Expiration')
        if expiration:
            expiration = datetime.fromisoformat(expiration)

        response = redirect('index')
        response.set_cookie(settings.CREDENTIALS_COOKIE_NAME, json.dumps(credentials),
            expires=expiration, httponly=True,
        )
        return response



# async def turk_question(request):
#     assignment_id = request.GET.get('assignmentId')
#     turk_submit_to = request.GET.get('turkSubmitTo')
#     worker_id = request.GET.get('workerId')
#     hit_id = request.GET.get('hitId')
#     turk_submit_url = turk_submit_to + "mturk/externalSubmit"
#     return HttpResponse(f"question_id: {question_id}")
