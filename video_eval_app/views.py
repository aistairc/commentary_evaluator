from icecream import ic # DEBUG: remove later

import json

from django.shortcuts import HttpResponseRedirect, render, redirect
from django.http import HttpResponse, JsonResponse
from django.core.paginator import Paginator
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_safe
from django.db.utils import IntegrityError
from django.db.models import Exists, OuterRef
from django.utils.safestring import SafeString
from guardian.shortcuts import assign_perm, remove_perm, get_objects_for_user, get_users_with_perms

from .models import *


ITEMS_PER_PAGE = 10

def req_pk(request):
    try:
        return int(request.POST["id"])
    except ValueError:
        return None

def upload_video(request, dataset):
    video = Video.get_or_create(file=request.FILES["file"])
    cuts = request.FILES.get('cuts')
    if cuts:
        with cuts.open('rt') as r:
            cuts_data = json.load(r)
    else:
        cuts_data = None
    dataset_video = DatasetVideo.objects.create(
        dataset=dataset,
        video=video,
        subtitles=request.FILES.get('subtitles'),
        audio=request.FILES.get('audio'),
        cuts=cuts_data,
        name=request.POST.get('name', ''),
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
    ).distinct()
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
        dataset = Dataset.objects.get(token=token)
        upload_video(request, dataset)
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
    paginator = Paginator(dataset.dataset_videos.all(), ITEMS_PER_PAGE)
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
            paginator = Paginator(dataset_video.segments.all(), ITEMS_PER_PAGE)
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
        upload_video(request, dataset)
        return redirect('dataset_videos', dataset_id=dataset.id)

@login_required
@require_safe
def dataset_projects(request, dataset_id):
    dataset = Dataset.objects.get(pk=dataset_id)
    manage_dataset_perm = request.user.has_perm('manage_dataset', dataset)
    if manage_dataset_perm:
        projects = dataset.projects.all()
    else:
        projects = get_objects_for_user(request.user, 'video_eval_app.manage_project').filter(dataset=dataset)
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
        return render(request, 'dataset_project.html', {
            'dataset': dataset,
            'project': project,
            'questions': SafeString(json.dumps(project.questions, ensure_ascii=False)),
            'turk_settings': SafeString(json.dumps(project.turk_settings, ensure_ascii=False)),
        })
    elif request.method == 'POST':
        defaults = {
            "name": request.POST["name"],
            "questions": json.loads(request.POST["questions"]),
            "turk_settings": json.loads(request.POST["turk_settings"]),
        }
        create_defaults={
            **defaults,
            "created_by": request.user,
            "dataset": dataset,
        }
        project, created = Project.objects.update_or_create(
            id=req_pk(request),
            defaults=defaults,
            create_defaults=create_defaults,
        )
        if created:
            assign_perm('manage_project', request.user, project)
            segments = Segment.objects.filter(dataset_video__dataset=project.dataset)
            tasks = [
                Task(project=project, segment=segment)
                for segment in segments
            ]
            Task.objects.bulk_create(tasks)
        return redirect('dataset_projects', dataset_id=dataset.id)

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
            .prefetch_related('dataset')
    )
    paginator = Paginator(projects, ITEMS_PER_PAGE)
    page_number = request.GET.get("page")
    page = paginator.get_page(page_number)
    page.object_list = [
        {
            "project": project,
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
        users = User.objects.filter(is_superuser=False).exclude(username='AnonymousUser')
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
        return render(request, 'dataset_managers.html', {
            'dataset': dataset,
            'page': page,
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
def project_users(request, project_id):
    project = Project.objects.get(pk=project_id)
    if not request.user.has_perm('manage_project', project):
        return HttpResponse('Forbidden', status=403)
    if request.method in {"GET", "HEAD"}:
        users = User.objects.filter(is_superuser=False).exclude(username='AnonymousUser')
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
        return render(request, 'project_users.html', {
            'dataset': project.dataset,
            'project': project,
            'page': page,
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
    worker, created = Worker.objects.get_or_create(user=request.user)
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
        'task': task,
        'dataset_video': task and task.segment.dataset_video,
    })

def detect_question_type(question):
    if 'options' not in question:
        return str
    types = list(set(type(option['value']) for option in question['options']))
    if len(types) != 1:
        return str
    return types[0]

def get_question_types(project):
    return {
        question["id"]: detect_question_type(question)
        for question in project.questions
    }

@require_POST
def task_eval_submit(request, task_id):
    task = Task.objects.get(pk=task_id)
    result = {}
    question_types = get_question_types(task.project)
    for question in task.project.questions:
        question_id = question['id']
        question_type = question_types[question_id]
        def converter(value):
            if question_type != str and not value:
                return None
            else:
                return question_type(value)
        if question['type'] == 'checkbox':
            answer = [
                converter(value)
                for value in request.POST.getlist(f'q-{question_id}')
            ]
        else:
            answer = converter(request.POST.get(f'q-{question_id}'))
        result[question_id] = answer
    Assignment.objects.create(
        task=task,
        worker=request.user.worker,
        status=Assignment.Status.LOCAL,
        segment_created_at=task.segment.created_at,
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
        task__project_id=project_id,
        status__in=[Assignment.Status.LOCAL, Assignment.Status.ACCEPTED]
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

# async def turk_question(request):
#     assignment_id = request.GET.get('assignmentId')
#     turk_submit_to = request.GET.get('turkSubmitTo')
#     worker_id = request.GET.get('workerId')
#     hit_id = request.GET.get('hitId')
#     turk_submit_url = turk_submit_to + "mturk/externalSubmit"
#     return HttpResponse(f"question_id: {question_id}")
