from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Pool, WaterReading, PoolAccess, OrganizationAccess
from .forms import WaterReadingForm
from django.core.paginator import Paginator
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import login
from django.views.decorators.csrf import csrf_protect
from django.shortcuts import render

def index(request):
    return render(request, 'pool_service/index.html')

@login_required
def pool_list(request):
    """Список бассейнов для пользователя"""
    if request.user.is_superuser:
        pools = Pool.objects.all()  # Администратор видит всё
    elif OrganizationAccess.objects.filter(user=request.user).exists():
        # Если пользователь сотрудник организации, показываем бассейны организации
        org_access = OrganizationAccess.objects.get(user=request.user)
        pools = Pool.objects.filter(organization=org_access.organization)
    else:
        # Иначе, пользователь должен быть клиентом бассейна
        pools = Pool.objects.filter(accesses__user=request.user)

    return render(request, "pool_service/pool_list.html", {"pools": pools})
    
def home(request):
    """
    Главная страница: общая информация о сайте и форма входа.
    Если форма отправлена и данные валидны, выполняется вход и перенаправление.
    """
    if request.method == 'POST':
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            # После входа перенаправляем на страницу со списком бассейнов
            return redirect('pool_list')
    else:
        form = AuthenticationForm()
        
    context = {
        'form': form,
        'info': "Добро пожаловать на наш сервис обслуживания бассейнов и водоочистки. Здесь вы можете узнать об услугах и войти в личный кабинет.",
    }
    return render(request, 'pool_service/home.html', context)

@login_required
def pool_detail(request, pool_id):
    """Детальная страница бассейна"""
    pool = get_object_or_404(Pool, id=pool_id)

    # Если суперпользователь, даем полный доступ
    if request.user.is_superuser:
        role = "admin"
    else:
        role = None

        # Если пользователь клиент бассейна
        # Проверяем доступы
        pool_access = PoolAccess.objects.filter(user=request.user, pool=pool).first()
        if pool_access:
            role = pool_access.role  # "client_read", "editor" и т. д.
        
        org_access = OrganizationAccess.objects.filter(user=request.user, organization=pool.organization).first()
        if org_access:
            role = org_access.role  # "manager", "service" и т. д.

    # Если роль не определена — доступ запрещен
    if not role:
        return render(request, "403.html")

    # Показания воды
    readings_list = WaterReading.objects.filter(pool=pool).order_by('-date')

    # Пагинация
    per_page = request.GET.get('per_page', 20)
    try:
        per_page = int(per_page)
    except ValueError:
        per_page = 20

    paginator = Paginator(readings_list, per_page)
    page_number = request.GET.get('page')
    readings = paginator.get_page(page_number)

    context = {
        'pool': pool,
        'readings': readings,
        'per_page': per_page,
        'role': role,  # Передаем роль пользователя
    }
    return render(request, "pool_service/pool_detail.html", context)


@csrf_protect
def water_reading_create(request, pool_id):
    """Форма добавления новых показаний воды"""
    pool = get_object_or_404(Pool, pk=pool_id)

    if request.method == 'POST':
        form = WaterReadingForm(request.POST)
        if form.is_valid():
            reading = form.save(commit=False)
            reading.date = reading.date.replace(tzinfo=None)  # Убираем временную зону
            reading.pool = pool
            reading.added_by = request.user
            reading.save()
            return redirect('pool_detail', pool_id=pool.id)
    else:
        form = WaterReadingForm()

    return render(request, 'pool_service/water_reading_form.html', {'form': form, 'pool': pool})
