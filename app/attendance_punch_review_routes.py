# ruff: noqa: B008
"""Read-only screen over raw fingerprint punches. Nothing is stored, no attendance is decided."""
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.attendance_import import parse_attendance_workbook, normalize as normalize_alias
from app.attendance_punch_review import build_punch_review
from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_attendance_read
from app.models import AdminAccount, Employee

MAX_BYTES = 5 * 1024 * 1024
NEEDS_REVIEW = ('duplicate_needs_hr_review', 'unreadable_needs_hr_review')
ERRORS = {
    'unsupported_extension': 'صيغة الملف غير مدعومة. استخدم XLS أو XLSX.',
    'empty_file': 'الملف فارغ.',
    'file_too_large': 'حجم الملف يجب ألا يتجاوز 5 ميجابايت.',
    'no_employees': 'لا يوجد موظفون مسجلون لمطابقة أكواد البصمة.',
    'ambiguous_employees': 'أكواد أو أسماء الموظفين متكررة ولا يمكن مطابقتها بأمان.',
    'unreadable_file': 'تعذر قراءة الملف كتقرير بصمات. راجع الملف الأصلي.',
}


def _aliases(db):
    aliases, owners = {}, {}
    for employee in db.scalars(select(Employee).order_by(Employee.id)).all():
        for value in (employee.code, employee.fingerprint_id, employee.full_name):
            if not value:
                continue
            key = normalize_alias(value)
            if owners.get(key) not in (None, employee.id):
                raise ValueError('ambiguous_employees')
            owners[key] = employee.id
            aliases[str(value)] = employee.id
    if not aliases:
        raise ValueError('no_employees')
    return aliases


def build_review_view(raw, filename, db):
    """Parse and review one upload; returns the template view. Never writes."""
    if Path(filename or '').suffix.casefold() not in {'.xls', '.xlsx'}:
        raise ValueError('unsupported_extension')
    if not raw:
        raise ValueError('empty_file')
    if len(raw) > MAX_BYTES:
        raise ValueError('file_too_large')
    aliases = _aliases(db)
    try:
        parsed = parse_attendance_workbook(raw, filename, 'fingerprint', aliases, preserve_duplicate_punches=True)
        review = build_punch_review(parsed)
    except Exception as exc:     # a corrupt workbook raises library-specific errors; show one plain message
        raise ValueError('unreadable_file') from exc
    names = {e.id: (e.code, e.full_name) for e in db.scalars(
        select(Employee).where(Employee.id.in_({d['employee_id'] for d in review['days']}))).all()}
    cards = []
    for day in review['days']:
        if day['review_state'] not in NEEDS_REVIEW:
            continue
        code, name = names.get(day['employee_id'], ('', ''))
        cards.append({**day, 'employee_code': code, 'employee_name': name,
                      'is_duplicate': day['review_state'] == 'duplicate_needs_hr_review',
                      'is_unreadable': day['review_state'] == 'unreadable_needs_hr_review',
                      'punch_times': [o[11:19] for o in day['occurrences']]})
    summary = review['summary']
    return {'cards': cards, 'total_days': summary['employee_days'], 'review_days': len(cards),
            'dropped': review['dropped_occurrences'], 'unmatched': summary['unmatched_identity_occurrences']}


def punch_review_router(templates):
    router = APIRouter()

    def render(request, admin, status_code=200, error=None, view=None):
        response = templates.TemplateResponse(
            request=request, name='attendance_punch_review.html', status_code=status_code,
            context={'admin': admin.display_name or admin.username, 'csrf_token': get_csrf_token(request),
                     'error': error, 'view': view})
        response.headers['Cache-Control'] = 'no-store'
        return response

    @router.get('/attendance/punch-review', response_class=HTMLResponse)
    def page(request: Request, admin: AdminAccount = Depends(require_attendance_read)):
        return render(request, admin)

    @router.post('/attendance/punch-review', response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
    def review(request: Request, file: UploadFile = File(None), admin: AdminAccount = Depends(require_attendance_read)):
        if file is None:
            return render(request, admin, 400, ERRORS['empty_file'])
        raw = file.file.read(MAX_BYTES + 1)
        try:
            with SessionLocal() as db:           # read-only: no commit, rolled back on close
                view = build_review_view(raw, file.filename or '', db)
        except ValueError as exc:
            return render(request, admin, 400, ERRORS.get(str(exc), ERRORS['unreadable_file']))
        return render(request, admin, view=view)
    return router
