import requests
import stripe
import firebase_admin
from firebase_admin import credentials, auth, messaging

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from core.customer import forms

from django.contrib import messages
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from django.conf import settings

from core.models import *

import openrouteservice

# cred = credentials.Certificate(settings.FIREBASE_ADMIN_CREDENTIAL)
# firebase_admin.initialize_app(cred)

stripe.api_key = settings.STRIPE_API_SECRET_KEY

@login_required()
def home(request):
    return redirect(reverse('customer:profile'))

@login_required(login_url="/sign-in/?next=/customer/")
def profile_page(request):
    user_form = forms.BasicUserForm(instance=request.user)
    customer_form = forms.BasicCustomerForm(instance=request.user.customer)
    password_form = PasswordChangeForm(request.user)

    if request.method == "POST":

        if request.POST.get('action') == 'update_profile':
            user_form = forms.BasicUserForm(request.POST, instance=request.user)
            customer_form = forms.BasicCustomerForm(request.POST, request.FILES, instance=request.user.customer)

            if user_form.is_valid() and customer_form.is_valid():
                user_form.save()
                customer_form.save()

                messages.success(request, 'Your profile has been updated')
                return redirect(reverse('customer:profile'))

        elif request.POST.get('action') == 'update_password':
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)

                messages.success(request, 'Your password has been updated')
                return redirect(reverse('customer:profile'))

        elif request.POST.get('action') == 'update_phone':
            # Get Firebase user data
            firebase_user = auth.verify_id_token(request.POST.get('id_token'))

            request.user.customer.phone_number = firebase_user['phone_number']
            request.user.customer.save()
            return redirect(reverse('customer:profile'))

    return render(request, 'customer/profile.html', {
        "user_form": user_form,
        "customer_form": customer_form,
        "password_form": password_form
    })

@login_required(login_url="/sign-in/?next=/customer/")
def payment_method_page(request):
    current_customer = request.user.customer

    if request.method == "POST":
        # Handling removal of existing payment method (if any)
        # In case of COD, you may not need to detach anything, just update the payment method
        current_customer.payment_method = 'COD'
        current_customer.save()
        return redirect(reverse('customer:payment_method'))

    # Check if current payment method is COD
    is_cash_on_delivery = current_customer.payment_method == 'COD'

    return render(request, 'customer/payment_method.html', {
        "is_cash_on_delivery": is_cash_on_delivery,
    })


@login_required(login_url="/sign-in/?next=/customer/")
def create_job_page(request):
    current_customer = request.user.customer

    # Redirect to payment method selection if payment method is not COD
    if current_customer.payment_method != 'COD':
        return redirect(reverse('customer:payment_method'))

    # Check if customer has a current job in processing status
    has_current_job = Job.objects.filter(
        customer=current_customer,
        status__in=[
            Job.PROCESSING_STATUS,
            Job.PICKING_STATUS,
            Job.DELIVERING_STATUS
        ]
    ).exists()

    if has_current_job:
        messages.warning(request, "You currently have a processing job.")
        return redirect(reverse('customer:current_jobs'))

    # Get the last creating job or create a new one
    creating_job = Job.objects.filter(customer=current_customer, status=Job.CREATING_STATUS).last()
    step1_form = forms.JobCreateStep1Form(instance=creating_job)
    step2_form = forms.JobCreateStep2Form(instance=creating_job)
    step3_form = forms.JobCreateStep3Form(instance=creating_job)

    if request.method == "POST":
        if request.POST.get('step') == '1':
            step1_form = forms.JobCreateStep1Form(request.POST, request.FILES, instance=creating_job)
            if step1_form.is_valid():
                creating_job = step1_form.save(commit=False)
                creating_job.customer = current_customer
                creating_job.save()
                return redirect(reverse('customer:create_job'))

        elif request.POST.get('step') == '2':
            step2_form = forms.JobCreateStep2Form(request.POST, instance=creating_job)
            if step2_form.is_valid():
                creating_job = step2_form.save()
                return redirect(reverse('customer:create_job'))

        elif request.POST.get('step') == '3':
            step3_form = forms.JobCreateStep3Form(request.POST, instance=creating_job)
            if step3_form.is_valid():
                creating_job = step3_form.save()
                
                try:
                    client = openrouteservice.Client(key=settings.OPENROUTESERVICE_API_KEY)
                    coordinates = [
                        [creating_job.pickup_lng, creating_job.pickup_lat],
                        [creating_job.delivery_lng, creating_job.delivery_lat]
                    ]
                    routes = client.directions(coordinates)

                    distance = routes['routes'][0]['summary']['distance']
                    duration = routes['routes'][0]['summary']['duration']
                    creating_job.distance = round(distance / 1000, 2)
                    creating_job.duration = int(duration / 60)
                    creating_job.price = creating_job.distance * 1  # $1 per km
                    creating_job.save()

                except Exception as e:
                    print(e)
                    messages.error(request, "Unfortunately, we do not support shipping at this distance")

                return redirect(reverse('customer:create_job'))

        elif request.POST.get('step') == '4':
            # Process the job as COD
            creating_job.status = Job.PROCESSING_STATUS
            creating_job.save()

            # Create transaction for COD payment
            Transaction.objects.create(
                job=creating_job,
                amount=creating_job.price,
                status=Transaction.OUT_STATUS  # Assuming the job is paid when created
            )

            # Send notification to couriers (you need to implement this part)
            # Example: messaging.send_notification_to_couriers(creating_job)

            return redirect(reverse('customer:home'))

    # Determine the current step
    if not creating_job:
        current_step = 1
    elif creating_job.delivery_name:
        current_step = 4
    elif creating_job.pickup_name:
        current_step = 3
    else:
        current_step = 2

    return render(request, 'customer/create_job.html', {
        "job": creating_job,
        "step": current_step,
        "step1_form": step1_form,
        "step2_form": step2_form,
        "step3_form": step3_form,
        "OPENROUTESERVICE_API_KEY": settings.OPENROUTESERVICE_API_KEY
    })


@login_required(login_url="/sign-in/?next=/customer/")
def current_jobs_page(request):
    jobs = Job.objects.filter(
        customer=request.user.customer,
        status__in=[
            Job.PROCESSING_STATUS,
            Job.PICKING_STATUS,
            Job.DELIVERING_STATUS
        ]
    )

    return render(request, 'customer/jobs.html', {
        "jobs": jobs
    })

@login_required(login_url="/sign-in/?next=/customer/")
def archived_jobs_page(request):
    jobs = Job.objects.filter(
        customer=request.user.customer,
        status__in=[
            Job.COMPLETED_STATUS,
            Job.CANCELED_STATUS
        ]
    )

    return render(request, 'customer/jobs.html', {
        "jobs": jobs
    })

@login_required(login_url="/sign-in/?next=/customer/")
def job_page(request, job_id):
    job = Job.objects.get(id=job_id)

    if request.method == "POST" and job.status == Job.PROCESSING_STATUS:
        # Call your API to cancel the job
        api_url = 'https://example.com/api/cancel-job/'  # Replace with your API endpoint
        api_data = {
            'job_id': job.id,
            # Add any other required data for your API call
        }

        try:
            response = requests.post(api_url, json=api_data)
            if response.status_code == 200:
                # Update job status locally
                job.status = Job.CANCELED_STATUS
                job.save()
                return redirect(reverse('customer:archived_jobs'))
            else:
                # Handle API error response
                error_message = response.json().get('error', 'Unknown error occurred.')
                return JsonResponse({'error': error_message}, status=response.status_code)
        except requests.RequestException as e:
            # Handle request exception
            return JsonResponse({'error': str(e)}, status=500)

    return render(request, 'customer/job.html', {
        "job": job,
    })