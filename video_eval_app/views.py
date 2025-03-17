from re import template
from icecream import ic # XXX: delete later

from django.contrib.admin.options import TemplateResponse, messages

import json
from datetime import datetime
from io import BytesIO, StringIO
import os
import hashlib
import random
import csv

from django.template.loader import render_to_string
from django.core.files import File
from django.conf import settings
from django.shortcuts import HttpResponseRedirect, render, redirect
from django.http import HttpResponse, JsonResponse, Http404
from django.core.paginator import Paginator
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_safe
from django.db import transaction
from django.db.utils import IntegrityError
from django.db.models import Exists, OuterRef, F, Count
from django.utils.safestring import SafeString
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from asgiref.sync import sync_to_async
from guardian.shortcuts import assign_perm, remove_perm, get_objects_for_user, get_users_with_perms
from invitations.utils import get_invitation_model
import botocore
import nh3
import chardet
from guardian.models import UserObjectPermission
from schema import SchemaError


from .models import *
from .tasks import get_assignments_from_mturk, post_project_to_mturk, cut_and_delocalize_video
from .mturk import MTurk, make_aws_session
from .utils import convert_answers, load_subtitles
from .json_schemata import parse_hit_type, parse_credentials, parse_questions
from .async_queue import AsyncQueue


Invitation = get_invitation_model()


ITEMS_PER_PAGE = 10



class NoCredentialsError(Exception): pass

ffmpeg_queue = AsyncQueue()

arender = sync_to_async(render)

@sync_to_async
def auser_has_perm(request, perm, obj):
    return request.user.has_perm(perm, obj)


async def get_request_credentials(request):
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
        async with session.client('s3') as s3:
            try:
                cors = await s3.get_bucket_cors(Bucket=bucket)
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
                await s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors)

    return credentials


async def upload_video(request, dataset, credentials):
    location = credentials and credentials.pop('Location')
    session = None
    if location:
        if credentials:
            session = make_aws_session(credentials)
        else:
            raise NoCredentialsError("S3 location has been requested but no AWS credentials were supplied")

    video = await StoredFile.store(request.FILES["file"], "video_files", session, location)
    audio = await StoredFile.store(request.FILES.get("audio"), "audio_files", session, location)
    if raw_subtitles_file := request.FILES.get('subtitles'):
        with raw_subtitles_file.open('rb') as r:
            raw_subs = r.read()
        subs = load_subtitles(sub_contents=raw_subs)
        subs_base, _ = os.path.splitext(raw_subtitles_file.name)
        subs_name = f"{subs_base}.vtt"
        subs_file = File(file=BytesIO(subs.content.encode()), name=subs_name)
    else:
        subs_file = None
    subtitles = await StoredFile.store(subs_file, "subs_files", session, location)
    cuts = request.FILES.get('cuts')
    if cuts:
        with cuts.open('rt') as r:
            cuts_data = json.load(r)
    else:
        cuts_data = None

    name = request.POST.get('name', '') or request.FILES["file"].name
    dataset_video, _created = await DatasetVideo.objects.aget_or_create(
        dataset=dataset,
        video=video,
        subtitles=subtitles,
        audio=audio,
        name=name,
        cuts=cuts_data,
    )
    await ffmpeg_queue(cut_and_delocalize_video, dataset_video, session, location)


def bulk_remove_perm(perm, query, obj):
    content_type = ContentType.objects.get_for_model(obj)
    UserObjectPermission.objects.filter(
        permission__codename=perm,
        user__in=query,
        content_type=content_type,
        object_pk=obj.pk,
    ).delete()


def set_perm_to_user_list(request, perm, obj):
    usernames = [username.strip() for username in request.POST[perm].split(',')]
    with transaction.atomic():
        bulk_remove_perm(perm, User.objects.all(), obj)
        if usernames[0]:
            assign_perm(perm, User.objects.filter(username__in=usernames), obj)

def get_user_list_for_perm(perms, perm):
    return ', '.join(sorted(
        user.username for user, permlist in perms.items() if perm in permlist
    ))

def secure_hash(input):
    return hashlib.sha256(input.encode()).hexdigest()

def get_task_list(tasks, request):
    return [
        {
            "task_id": task.id,
            "video_url": task.segment.video.absolute_url(request),
            "subtitles_url": task.segment.subtitles and task.segment.subtitles.absolute_url(request),
        }
        for task in tasks
    ]

# dataset, project, template_vars = get_menu_data(request, dataset_id, project_id)
def get_menu_data(request, dataset_id=None, project_id=None, assignment=None):
    user = request.user
    if assignment:
        project = assignment.task.project
        project_id = assignment.task.project_id
        dataset = project.dataset
        dataset_id = dataset.id
    else:
        project = project_id and Project.objects.get(pk=project_id)
        if project_id and not dataset_id:
            dataset_id = project.dataset_id
        dataset = dataset_id and Dataset.objects.get(pk=dataset_id)
    dataset_filter = { "dataset": dataset } if dataset else {}
    manage_project_ids = set(
        get_objects_for_user(user, 'video_eval_app.manage_project')
            .filter(**dataset_filter)
            .values_list('id', flat=True)
    )
    evaluate_project_ids = set(
        get_objects_for_user(user, 'video_eval_app.evaluate_project')
            .filter(**dataset_filter)
            .values_list('id', flat=True)
    )
    manage_dataset_ids = set(
        get_objects_for_user(user, 'video_eval_app.manage_dataset')
            .values_list('id', flat=True)
    )
    can_add_dataset = user.has_perm('video_eval_app.add_dataset')
    if evaluate_project_ids:
        worker = Worker.objects.filter(user=user).first()
        evaluation_tasks = {
            item['project_id']: item['count'] for item in
            Task.objects.filter(
                ~Exists(Assignment.objects.filter(
                    task_id=OuterRef('pk'),
                    worker=worker,
                )),
                project_id__in=evaluate_project_ids,
            ).values('project_id').annotate(count=Count('id'))
        }
    else:
        evaluation_tasks = None

    template_vars = {
        'dataset_id': dataset_id,
        'dataset': dataset,
        'project_id': project_id,
        'project': project,
        'manage_project_ids': manage_project_ids,
        'evaluate_project_ids': evaluate_project_ids,
        'manage_dataset_ids': manage_dataset_ids,
        'can_add_dataset': can_add_dataset,
        'evaluation_tasks': evaluation_tasks,
    }
    return dataset, project, template_vars

aget_menu_data = sync_to_async(get_menu_data)


def index(request):
    _dataset, _project, template_vars = get_menu_data(request)
    return render(request, 'index.html', template_vars)

@login_required
@require_safe
def datasets(request):
    _dataset, _project, template_vars = get_menu_data(request)
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
        **template_vars,
    })

@login_required
def datasets_new(request):
    _dataset, _project, template_vars = get_menu_data(request)
    can_add_dataset = template_vars['can_add_dataset']
    if not can_add_dataset:
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        return render(request, 'datasets_new.html', template_vars)
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
    dataset, _project, template_vars = get_menu_data(request, dataset_id)
    manage_dataset_perm = dataset_id in template_vars['manage_dataset_ids']
    if not manage_dataset_perm:
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        upload_video_url = request.user.is_authenticated and request.build_absolute_uri(reverse('upload_video_api', args=[dataset.token]))
        return render(request, 'dataset_edit.html', {
            'editable': manage_dataset_perm,
            'upload_video_url': manage_dataset_perm and upload_video_url,
            **template_vars,
        })
    elif request.method == 'POST':
        dataset.name = request.POST['name']
        if request.POST['renew'] == '1':
            dataset.renew_token()
        dataset.save()
        return redirect(request.path_info)

@csrf_exempt
@require_POST
async def upload_video_api(request, token):
    try:
        credentials = await get_request_credentials(request)
        dataset = await Dataset.objects.aget(token=token)
        await upload_video(request, dataset, credentials)
        return HttpResponse(status=204)
    except Dataset.DoesNotExist:
        return HttpResponse("Invalid token.\n", status=403)
    # except IntegrityError:
    #     return HttpResponse("This video is already in this dataset.\n", status=400)

@login_required
@require_safe
def dataset_videos(request, dataset_id):
    dataset, _project, template_vars = get_menu_data(request, dataset_id)
    manage_dataset_perm = dataset_id in template_vars['manage_dataset_ids']
    managed_projects = dataset.projects.filter(id__in=template_vars['manage_project_ids'])
    if not (manage_dataset_perm or managed_projects):
        return HttpResponse('Forbidden', status=403)
    paginator = Paginator(dataset.dataset_videos.order_by('name').all(), ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    new_url = request.user.is_authenticated and reverse('dataset_videos_new', args=[dataset.id])
    return render(request, 'dataset_videos.html', {
        'page': page,
        'new_url': new_url,
        **template_vars,
    })

@login_required
async def dataset_video(request, dataset_id, dataset_video_id=None):
    dataset, _project, template_vars = await aget_menu_data(request, dataset_id)
    manage_dataset_perm = dataset_id in template_vars['manage_dataset_ids']
    managed_projects = dataset.projects.filter(id__in=template_vars['manage_project_ids'])
    if not (manage_dataset_perm or managed_projects):
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        if dataset_video_id:
            dataset_video = await DatasetVideo.objects.aget(pk=dataset_video_id, dataset=dataset)
            paginator = Paginator(dataset_video.segments.order_by('start'), ITEMS_PER_PAGE)
            page_number = request.GET.get("page")
            page = await sync_to_async(paginator.get_page)(page_number)
        else:
            dataset_video = DatasetVideo(dataset=dataset)
            page = None
        return await arender(request, 'dataset_video.html', {
            'editable': manage_dataset_perm,
            'dataset_video': dataset_video,
            'page': page,
            **template_vars,
        })
    elif request.method == 'POST':
        await upload_video(request, dataset, request.credentials)
        return redirect('dataset_videos', dataset_id=dataset.id)

@login_required
@require_safe
def dataset_projects(request, dataset_id):
    dataset, _project, template_vars = get_menu_data(request, dataset_id)
    manage_dataset_perm = dataset_id in template_vars['manage_dataset_ids']
    if manage_dataset_perm:
        projects = dataset.projects.order_by('name').all()
    else:
        projects = dataset.projects.filter(id__in=template_vars['manage_project_ids']).order_by('name')
    paginator = Paginator(projects, ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    new_url = manage_dataset_perm and reverse('dataset_project_new', args=[dataset.id])
    return render(request, 'dataset_projects.html', {
        "page": page,
        "new_url": new_url,
        **template_vars,
    })

@login_required
async def dataset_project(request, dataset_id, project_id=None):
    dataset, project, template_vars = await aget_menu_data(request, dataset_id, project_id)
    if project_id:
        if project_id not in template_vars['manage_project_ids']:
            return HttpResponse('Forbidden', status=403)
    else:
        if dataset_id not in template_vars['manage_dataset_ids']:
            return HttpResponse('Forbidden', status=403)
        project = Project()
        template_vars['project'] = project
    if request.method in {"GET", "HEAD"}:
        if project_id:
            num_approved_assignments = await Assignment.objects.filter(
                task__project=project, is_approved=True,
            ).acount()
        else:
            num_approved_assignments = None
        num_uncut_videos = await dataset.dataset_videos.filter(is_cut=False).acount() # TODO: disable "Start" button if >0, show an info message
        if project.turk_hit_group_id and request.credentials:
            preview_url = MTurk.get_environment()['preview'] + '?groupId=' + project.turk_hit_group_id
        else:
            preview_url = None
        questions = SafeString(json.dumps(project.questions, ensure_ascii=False)) if project.questions else ""
        turk_settings = SafeString(json.dumps(project.turk_settings, ensure_ascii=False)) if project.turk_settings else ""
        return await arender(request, 'dataset_project.html', {
            'project_messages': project.messages,
            'identity_choices': Project.WorkerIdentity.choices,
            'questions': questions,
            'turk_settings': turk_settings,
            'num_uncut_videos': num_uncut_videos,
            'preview_url': preview_url,
            'busy_disabled': 'disabled' if project.is_busy else '',
            'cred_busy_disabled': 'disabled' if project.is_busy or not request.credentials else '',
            'num_approved_assignments': num_approved_assignments,
            **template_vars,
        })
    elif request.method == 'POST':
        if project.is_busy:
            messages.warning(request, 'The project became busy, the operation was not performed. Please try again later.')
        elif request.POST.get('dismiss_messages'):
            project.messages = []
            await project.asave()
        elif request.POST.get('collect_mturk'):
            await get_assignments_from_mturk(project, request.credentials)
        else:
            turk_settings_text = request.POST["turk_settings"].strip()
            if turk_settings_text:
                try:
                    turk_settings = parse_hit_type(turk_settings_text)
                except (SchemaError, JSONDecodeError) as x:
                    messages.error(request, str(x))
                    return redirect(request.path_info)
            else:
                turk_settings = None
            will_submit_to_mturk = bool(turk_settings_text)

            num_uncut_videos = await dataset.dataset_videos.filter(is_cut=False).acount()
            if num_uncut_videos:
                messages.warning(request, f'{num_uncut_videos} video(s) still being processed')
                return redirect(request.path_info)

            name = request.POST["name"].strip()
            if not name:
                messages.error(request, 'Name cannot be empty')
                return redirect(request.path_info)

            questions_text = request.POST["questions"].strip()
            if questions_text:
                try:
                    questions = parse_questions(questions_text)
                except (SchemaError, json.decoder.JSONDecodeError) as x:
                    messages.error(request, str(x))
                    return redirect(request.path_info)
            else:
                messages.error(request, "A project must have at least one question")
                return redirect(request.path_info)

            for question in questions:
                question["instruction"] = nh3.clean(question["instruction"])
                if options := question.get("options"):
                    for option in options:
                        option["text"] = nh3.clean(option["text"])

            defaults = {
                "name": name,
                "worker_identity": request.POST["identity"],
                "questions": questions,
                "turk_settings": turk_settings,
            }
            create_defaults = {
                **defaults,
                "created_by": request.user,
                "dataset": dataset,
                "is_busy": will_submit_to_mturk,
            }
            project, created = await Project.objects.aupdate_or_create(
                id=project_id,
                defaults=defaults,
                create_defaults=create_defaults,
            )
            if created:
                await sync_to_async(assign_perm)('manage_project', request.user, project)

            if request.POST.get('start'):
                if will_submit_to_mturk and not request.credentials:
                    messages.error(request, "The project has MTurk settings but no AWS credentials are supplied")
                    return redirect(request.path_info)

                if will_submit_to_mturk:
                    # get MTurk client and check if it works
                    mturk = MTurk(request.credentials)
                    await mturk.get_account_balance()

                segments = Segment.objects.filter(dataset_video__dataset_id=project.dataset_id)
                tasks = [
                    Task(project=project, segment=segment)
                    async for segment in segments
                ]
                await Task.objects.abulk_create(tasks)

                if will_submit_to_mturk:
                    tasks = await sync_to_async(project.tasks.prefetch_related)('segment', 'project')
                    task_list = await sync_to_async(get_task_list)(tasks, request)
                    await post_project_to_mturk(project, task_list, mturk)

                await Project.objects.filter(pk=project.id).aupdate(is_started=True)

        return redirect('dataset_project', dataset_id=dataset.id, project_id=project.id)

@login_required
@require_safe
def projects(request):
    _dataset, _project, template_vars = get_menu_data(request)
    projects = (
        Project.objects.filter(
            pk__in=(template_vars['evaluate_project_ids'] | template_vars['manage_project_ids'])
        ).prefetch_related('dataset').order_by('name')
    )
    paginator = Paginator(projects, ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    page.object_list = [
        {
            "project": project,
            # TODO: we could rewrite this
            "evaluate": True,
            "manage_project_perm": project.id in template_vars['manage_project_ids'],
            "evaluate_project_perm": project.id in template_vars['evaluate_project_ids'],
            "manage_dataset_perm": project.dataset.id in template_vars['manage_dataset_ids'],
        }
        for project in page.object_list
    ]
    return render(request, 'projects.html', {
        'page': page,
        **template_vars,
    })

@login_required
def creators(request):
    _dataset, _project, template_vars = get_menu_data(request)
    if not request.user.is_staff:
        return HttpResponse('Forbidden', status=403)
    content_type = ContentType.objects.get_for_model(Dataset)
    add_permission = Permission.objects.get(content_type=content_type, codename='add_dataset')
    if request.method in {"GET", "HEAD"}:
        users = User.objects.filter(is_superuser=False).exclude(username='AnonymousUser').order_by('username').annotate(
            is_creator=Exists(
                User.user_permissions.through.objects.filter(user_id=OuterRef('id'), permission=add_permission)
            )
        )
        paginator = Paginator(users, ITEMS_PER_PAGE)
        page_number = request.GET.get("page")
        page = paginator.get_page(page_number)
        new_url = request.user.has_perm('add_user') and reverse('creator_invite', args=[])
        return render(request, 'creators.html', {
            'page': page,
            'new_url': new_url,
            **template_vars,
        })
    elif request.method == 'POST':
        is_creator = request.POST.get('creator')
        is_staff = request.POST.get('staff')
        user_id = request.POST["user_id"]
        user = User.objects.get(pk=user_id)
        if is_creator:
            if is_creator == 'False':
                user.user_permissions.add(add_permission)
            else:
                user.user_permissions.remove(add_permission)
            user.save()
        elif is_staff:
            user.is_staff = is_staff == 'False'
            user.save()
        return redirect('creators')

@login_required
def dataset_managers(request, dataset_id):
    dataset, _project, template_vars = get_menu_data(request, dataset_id)
    manage_dataset_perm = dataset_id in template_vars['manage_dataset_ids']
    if not manage_dataset_perm:
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
            'page': page,
            'new_url': new_url,
            'managers': get_user_list_for_perm(perms, 'manage_dataset'),
            **template_vars,
        })
    elif request.method == 'POST':
        if 'manage_dataset' in request.POST:
            set_perm_to_user_list(request, 'manage_dataset', dataset)
        else:
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
    dataset, project, template_vars = get_menu_data(request, None, project_id)
    # TODO: check permissions
    assignments = Assignment.objects.filter(task__project=project).order_by('task__segment__dataset_video__name', 'task__segment__start')
    paginator = Paginator(assignments, ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    return render(request, 'project_approvals.html', {
        'page': page,
        **template_vars,
    })

@login_required
@require_POST
async def assignment_approve_all(request, project_id):
    dataset, project, template_vars = await aget_menu_data(request, project_id=project_id)
    manage_project_perm = project_id in template_vars['manage_project_ids']
    if not manage_project_perm:
        return HttpResponse('Forbidden', status=403)
    await Assignment.objects.filter(
        task__project=project,
        is_approved__isnull=True,
        turk_assignment_id__isnull=True,
    ).aupdate(is_approved=True)
    mturk = MTurk(request.credentials)
    if project.turk_settings:
        mturk = MTurk(request.credentials)
        async with mturk.connect() as client:
            turk_assignments = Assignment.objects.filter(
                task__project=project,
                is_approved__isnull=True,
                turk_assignment_id__isnull=False,
            )
            async for assignment in turk_assignments:
                response = await client.approve_assignment(
                    AssignmentId=assignment.turk_assignment_id,
                )
                ic(response)
    await Assignment.objects.filter(
        task__project=project,
        is_approved__isnull=True,
    ).aupdate(is_approved=True)
    return redirect('project_approvals', project_id=project_id)

@login_required
async def assignment(request, assignment_id):
    assignment = await Assignment.objects.filter(pk=assignment_id).prefetch_related('task__project__dataset').afirst()
    dataset, project, template_vars = await aget_menu_data(request, assignment=assignment)
    # TODO: check permissions
    if request.method == 'POST':
        feedback = request.POST.get('feedback')
        feedback_opt = {}
        if feedback:
            feedback_opt['RequesterFeedback'] = feedback
        if 'approve' in request.POST:
            if assignment.turk_assignment_id:
                mturk = MTurk(request.credentials)
                async with mturk.connect() as client:
                    response = await client.approve_assignment(
                        AssignmentId=assignment.turk_assignment_id,
                        **feedback_opt,
                        OverrideRejection=True,
                    )
            assignment.is_approved = True
            await assignment.asave()
        elif 'reject' in request.POST:
            if assignment.turk_assignment_id:
                if not feedback:
                    messages.error(request, 'MTurk rejection requires feedback')
                    return HttpResponseRedirect(request.path_info)
                if assignment.is_approved:
                    messages.error(request, 'Cannot reject an already approved MTurk assignment')
                    return HttpResponseRedirect(request.path_info)
                mturk = MTurk(request.credentials)
                async with mturk.connect() as client:
                    response = await client.reject_assignment(
                        AssignmentId=assignment.turk_assignment_id,
                        **feedback_opt,
                    )
            assignment.is_approved = False
            await assignment.asave()
        return redirect('project_approvals', project_id=assignment.task.project.id)
    else:
        def load_project_segment_and_other_required_data():
            segment = assignment.task.segment
            segment.video
            segment.subtitles
            return segment
        segment = await sync_to_async(load_project_segment_and_other_required_data)()
        return await arender(request, 'assignment.html', {
            'task_id': assignment.task.id,
            'video_url': segment.video.absolute_url(request),
            'subtitles_url': segment.subtitles and segment.subtitles.absolute_url(request),
            'assignment': assignment,
            **template_vars,
        })

@login_required
def project_users(request, project_id):
    dataset, project, template_vars = get_menu_data(request, None, project_id)
    manage_project_perm = project_id in template_vars['manage_project_ids']
    if not manage_project_perm:
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
            'page': page,
            'new_url': new_url,
            'evaluators': get_user_list_for_perm(perms, 'evaluate_project'),
            'managers': get_user_list_for_perm(perms, 'manage_project'),
            **template_vars,
        })
    elif request.method == 'POST':
        if 'evaluate_project' in request.POST:
            set_perm_to_user_list(request, 'evaluate_project', project)
            set_perm_to_user_list(request, 'manage_project', project)
        else:
            user_id = request.POST["user_id"]
            manage = request.POST.get("manage")
            evaluate = request.POST.get("evaluate")
            user = User.objects.get(pk=user_id)
            if evaluate:
                if evaluate == 'False':
                    assign_perm('evaluate_project', user, project)
                else:
                    remove_perm('evaluate_project', user, project)
            elif manage:
                if manage == 'False':
                    assign_perm('manage_project', user, project)
                else:
                    remove_perm('manage_project', user, project)
        return redirect('project_users', project_id=project.id)

@login_required
def invite_user(request, dataset_id=None, project_id=None):
    dataset, project, template_vars = get_menu_data(request, dataset_id, project_id)
    if project_id:
        manage_project_perm = project_id in template_vars['manage_project_ids']
        if not manage_project_perm:
            return HttpResponse('Forbidden', status=403)
    elif dataset_id:
        manage_dataset_perm = dataset_id in template_vars['manage_dataset_ids']
        if not manage_dataset_perm:
            return HttpResponse('Forbidden', status=403)
    else:
        if not request.user.is_staff:
            return HttpResponse('Forbidden', status=403)
    if request.method in {'GET', 'HEAD'}:
        return render(request, 'invite_user.html', template_vars)
    elif request.method == 'POST':
        role = request.POST['role']
        email = request.POST['email']
        invitation = Invitation.objects.filter(email__iexact=email).last()
        if invitation:
            pass # TODO: refuse to send duplicate invitation
        invitation = Invitation.create(email=email, role=role, dataset=dataset, project=project)
        invitation.send_invitation(request)
        if project_id:
            return redirect('project_users', project_id=project_id)
        else:
            return redirect('dataset_managers', dataset_id=dataset_id)

@login_required
def segment(request, segment_id):
    segment = Segment.objects.get(pk=segment_id)
    dataset_id = segment.dataset_video.dataset_id
    dataset, _project, template_vars = get_menu_data(request, dataset_id)
    manage_dataset_perm = dataset_id in template_vars['manage_dataset_ids']
    managed_projects = dataset.projects.filter(id__in=template_vars['manage_project_ids'])
    if not (manage_dataset_perm or managed_projects): # TODO: or a project
        return HttpResponse('Forbidden', status=403)
    return render(request, 'segment.html', {
        'segment': segment,
        **template_vars,
    })

@login_required
@require_safe
def project_eval(request, project_id):
    dataset, project, template_vars = get_menu_data(request, None, project_id)
    evaluate_project_perm = project_id in template_vars['evaluate_project_ids']
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
        # TODO: remind me, what is evaluate?
        'evaluate': True,
        'dataset_video': task and task.segment.dataset_video,
        'task_id': task and task.id,
        'video_url': task and task.segment.video.url,
        'subtitles_url': task and task.segment.subtitles and task.segment.subtitles.url,
        **template_vars,
    })

@require_POST
def task_eval_submit(request, task_id):
    task = Task.objects.get(pk=task_id)
    # TODO: check permissions
    result = convert_answers(task.project.questions, request=request)
    Assignment.objects.create(
        task=task,
        worker=request.user.worker,
        result=result,
    )
    return redirect('project_eval', project_id=task.project.id)

@login_required
def project_external(request, project_id):
    dataset, project, template_vars = get_menu_data(request, None, project_id)
    ic(project_id)
    ic(template_vars['manage_project_ids'])
    manage_project_perm = project_id in template_vars['manage_project_ids']
    ic(manage_project_perm)
    if not manage_project_perm:
        return HttpResponse('Forbidden', status=403)
    if request.method in {'GET', 'HEAD'}:
        return render(request, 'project_external.html', {
            'list_formats': settings.EXTERNAL_LIST_FORMATS,
            'var_formats': settings.EXTERNAL_VAR_FORMATS,
            **template_vars,
        })
    elif request.method == 'POST':
        results_file = request.FILES.get('results')
        if not results_file:
            messages.error(request, 'No results file was submitted')
            return redirect(request.path_info)
        byte_contents = results_file.read()
        encoding = chardet.detect(byte_contents)['encoding'] 
        text_contents = byte_contents.decode(encoding)
        sniffer = csv.Sniffer()
        has_headers = sniffer.has_header(text_contents)
        if not has_headers:
            messages.error(request, 'The uploaded CSV/TSV file does not have a header')
            return redirect(request.path_info)
        dialect = sniffer.sniff(text_contents)
        csv_reader = csv.DictReader(StringIO(text_contents), dialect=dialect)
        worker_id_field = next(
            (
                fieldname
                for fieldname in settings.EXTERNAL_WORKER_ID_FIELD.keys()
                if fieldname in csv_reader.fieldnames
            ),
            None
        )
        service = settings.EXTERNAL_WORKER_ID_FIELD.get(worker_id_field, '')
        if not worker_id_field:
            messages.error(request, 'The uploaded CSV/TSV file does not have an identifiable worker ID field')
            return redirect(request.path_info)
        if 'task_id' not in csv_reader.fieldnames:
            messages.error(request, 'The uploaded CSV/TSV file does not have an identifiable worker ID field')
            return redirect(request.path_info)
        for row in csv_reader:
            worker_id = row[worker_id_field]
            task_id = row['task_id']
            results = convert_answers(project.questions, turk_answers=row)
            worker, _ = Worker.objects.get_or_create(
                worker_id=worker_id, service=service
            )
            assignment, _ = Assignment.objects.update_or_create(
                task_id=task_id,
                worker=worker,
                defaults={
                    "result": results,
                    "is_approved": None,
                }
            )
        return redirect('project_approvals', project_id=project.id)

@login_required
@require_safe
def external_template(request, project_id):
    # not using layout, so full get_menu_data is not needed
    project = Project.objects.get(pk=project_id)
    if not request.user.has_perm('video_eval_app.manage_project', project):
        return HttpResponse('Forbidden', status=403)

    var_format_id = request.GET["var-format"]
    var_format_obj = settings.EXTERNAL_VAR_FORMATS[var_format_id]
    var_format = var_format_obj.get('form')
    if not var_format:
        var_format = var_format_obj['name'].replace('var', '%s')
    subtitles_url_var = var_format % 'subtitlesUrl'
    video_url_var = var_format % 'videoUrl'
    task_var = var_format % 'task'
    question = render_to_string('external_question.html', {
        "project": project,
        "task_id": task_var,
        "video_url": video_url_var,
        "subtitles_url": subtitles_url_var,
    })
    response = HttpResponse(question, content_type='text/plain')
    response['Content-Disposition'] = 'inline; filename="template.txt"'
    return response

@login_required
@require_safe
def external_datalist(request, project_id):
    # not using layout, so full get_menu_data is not needed
    project = Project.objects.get(pk=project_id)
    if not request.user.has_perm('video_eval_app.manage_project', project):
        return HttpResponse('Forbidden', status=403)

    list_format_id = request.GET["list-format"]
    list_format_obj = settings.EXTERNAL_LIST_FORMATS[list_format_id]
    list_format_opts = list_format_obj.get('opts', {})
    list_format_ext = list_format_obj.get('ext', 'txt')
    list_format_mime = list_format_obj.get('mime', 'text/plain') # because Chrome >.<
    data = [
        {
            "taskId": task.id,
            "videoUrl": task.segment.video.absolute_url(request),
            "subtitlesUrl": task.segment.subtitles and task.segment.subtitles.absolute_url(request),
        }
        for task in project.tasks.all()
    ]
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=("taskId", "videoUrl", "subtitlesUrl"), **list_format_opts)
    writer.writeheader()
    writer.writerows(data)
    response = HttpResponse(output.getvalue(), content_type='text/plain')
    response['Content-Disposition'] = f'inline; filename="datalist.{list_format_ext}"'
    response['Content-Type'] = list_format_mime
    return response


@login_required
@require_safe
def project_results(request, project_id):
    # not using layout, so full get_menu_data is not needed
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
    identity = project.worker_identity
    anonymous = identity == Project.WorkerIdentity.ANONYMOUS
    # if not anonymous:
    #     assignments = assignments.annotate(
    #         username=F('worker__user__username'),
    #         resolved_worker_id=F('worker__worker_id'),
    #     )
    results = {
        "project": project.name,
        "tasks": {
            task.id: {
                "name": task.segment.dataset_video.name,
                "start": task.segment.start,
                "end": task.segment.end,
                "evaluations": [] if anonymous else {},
            }
            for task in tasks
        },
    }
    if not anonymous:
        workers = {
            # assignment.worker_id: assignment.username or f"TURK:{assignment.resolved_worker_id}"
            assignment.worker_id: str(assignment.worker)
            for assignment in assignments
        }
        worker_ids = list(workers)
        random.shuffle(worker_ids)
        for ix, worker_id in enumerate(worker_ids):
            if identity == Project.WorkerIdentity.NUMBERED:
                workers[worker_id] = ix
            elif identity == Project.WorkerIdentity.HASHED:
                workers[worker_id] = secure_hash(f"WORKER:{worker_id}:{settings.SECRET_KEY}")
    for assignment in assignments:
        evaluations = results["tasks"][assignment.task_id]["evaluations"]
        if anonymous:
            evaluations.append(assignment.result)
        else:
            evaluations[workers[assignment.worker_id]] = assignment.result

    results["tasks"] = list(results["tasks"].values())
    return HttpResponse(
        json.dumps(results, ensure_ascii=False),
        content_type="application/json"
    )

def accept_invite(request, key):
    # not using layout, so full get_menu_data is not needed
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

        # If neither project nor dataset are specified, the user should be staff
        scope = invitation.project or invitation.dataset
        user = User.objects.create_user(
            username=request.POST['username'],
            email=invitation.email,
            password=password,
        )

        if scope:
            assign_perm(invitation.role, user, scope)
        else:
            content_type = ContentType.objects.get_for_model(Dataset)
            add_permission = Permission.objects.get(content_type=content_type, codename='add_dataset')
            user.user_permissions.add(add_permission)

        login(request, user, backend='django.contrib.auth.backends.ModelBackend')

        invitation.accepted = True
        invitation.save()

        return redirect('index')

@login_required
async def credentials(request):
    _dataset, _project, template_vars = await aget_menu_data(request)
    if request.method in {"GET", "HEAD"}:
        location = ''
        if request.credentials:
            location = request.credentials.pop('Location', '')
        credentials_text = SafeString(json.dumps(request.credentials, indent=4)) if request.credentials else ''
        return await arender(request, 'credentials.html', {
            "credentials": credentials_text,
            "location": location,
            **template_vars,
        })
    elif request.method == 'POST':
        try:
            credentials = await get_request_credentials(request)
        except ValueError:
            messages.error(request, 'The credentials are not a valid JSON')
            return redirect(request.path_info)

        try:
            mturk = MTurk(credentials)
            async with mturk.connect() as client:
                await mturk.get_account_balance()
        except botocore.exceptions.ClientError:
            messages.error(request, 'AWS could not use the credentials')
            return redirect(request.path_info)

        location = credentials.get("Location")
        if location:
            key = 'video_eval_test_file.txt'
            body = b'Test'
            bucket, path = location.split('/', 1)
            if path:
                key = f"{path}/{key}"

            session = make_aws_session(credentials)
            async with session.client('s3') as s3:
                try:
                    await s3.put_object(Bucket=bucket, Key=key, Body=body, ACL='public-read')
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
