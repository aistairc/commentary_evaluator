from django.shortcuts import HttpResponseRedirect, render
from django.http import HttpResponse


def index(request):
    return render(request, 'index.html')

def turk_question(request):
    assignment_id = request.GET.get('assignmentId')
    turk_submit_to = request.GET.get('turkSubmitTo')
    # worker_id = request.GET.get('workerId')
    # hit_id = request.GET.get('hitId')
    turk_submit_url = turk_submit_to + "mturk/externalSubmit"
    return HttpResponse(f"question_id: {question_id}")
