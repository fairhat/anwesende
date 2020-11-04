import collections
import os
import tempfile
import typing as tg

import django.db.models.query as djdmq
from django.conf import settings

import anwesende.room.models as arm
import anwesende.users.models as aum
import anwesende.utils.date as aud
import anwesende.utils.excel as aue


class InvalidExcelError(ValueError):
    pass  # no additional logic is needed


def validate_excel_importfile(filename) -> None:
    # may raise InvalidExcelError
    columnsdict = aue.read_excel_as_columnsdict(filename)
    _validate_room_declarations(columnsdict)


def create_seats_from_excel(filename: str, user: aum.User) -> arm.Importstep:
    columnsdict = aue.read_excel_as_columnsdict(filename)
    _validate_room_declarations(columnsdict)
    importstep = _create_importstep(user)
    rooms, importstep.num_new_rooms, importstep.num_existing_rooms = \
        _find_or_create_rooms(columnsdict, importstep)
    seats, importstep.num_new_seats, importstep.num_existing_seats = \
        _find_or_create_seats(rooms)
    importstep.save()
    return importstep


def _validate_room_declarations(columndict: aue.Columnsdict):
    if getattr(columndict, 'has_been_validated', False): 
        return
    _validate_columnlist(columndict)
    _validate_single_department(columndict)
    _validate_seatrange(columndict)
    columndict.has_been_validated = True  # type: ignore


def _validate_columnlist(columndict: aue.Columnsdict):
    expected = ('organization', 'department', 'building', 'room',
                'seat_min', 'seat_max')
    found = columndict.keys()
    missing = set(expected) - set(found)
    if missing:
        _excelerror(row=1, expected=f"columns {expected}",
                    found=f"some columns are missing: {missing}")
    surprises = set(found) - set(expected)
    if surprises:
        _excelerror(row=1, expected=f"columns {expected}",
                    found=f"there are additional columns: {surprises}")


def _validate_single_department(columndict: aue.Columnsdict):
    organizations = set(columndict['organization'])
    departments = set(columndict['department'])
    if len(organizations) > 1:
        _excelerror(column='organization',
                    expected="all values are the same",
                    found=f"multiple different values: {organizations}")
    if len(departments) > 1:
        _excelerror(column='department',
                    expected="all values are the same",
                    found=f"multiple different values: {departments}")


def _validate_seatrange(columndict: aue.Columnsdict):
    mins = columndict['seat_min']
    maxs = columndict['seat_max']
    for index in range(len(columndict['seat_min'])):
        excel_row_number = index + 2
        try:
            _min = int(mins[index])
        except ValueError:
            _excelerror(row=excel_row_number, column='seat_min',
                        found=f"Not an integer number: {mins[index]}")
        try:
            _max = int(maxs[index])
        except ValueError:
            _excelerror(row=excel_row_number, column='seat_max',
                        found=f"Not an integer number: {maxs[index]}")
        if _min > _max:
            _excelerror(row=excel_row_number, column="seat_min/seat_max",
                        expected="seat_max is larger than seat_min",
                        found=f"seat_min={_min}, seat_max={_max}")
        seatsN = _max - _min
        if seatsN > 200:
            remark = ("if you are serious, use several lines with "
                      "sub-rooms left/center/right or so")
            _excelerror(row=excel_row_number,
                        expected="No room has more than 200 seats in pandemic times",
                        found=f"{seatsN} seats, from {_min} to {_max} ({remark})")


def _create_importstep(user: aum.User) -> arm.Importstep:
    result = arm.Importstep(user=user)
    result.save()
    return result


def _find_or_create_rooms(
        columnsdict: aue.Columnsdict,
        importstep: arm.Importstep) -> tg.Tuple[tg.Sequence[arm.Room], int, int]:
    _validate_room_declarations(columnsdict)
    result = []
    newN = existingN = 0
    for idx in range(len(columnsdict['room'])):
        def col(name): 
            return columnsdict[name][idx] 
        room, created = arm.Room.objects.get_or_create(
            organization=col('organization'),
            department=col('department'),
            building=col('building'),
            room=col('room'),
            defaults=dict(seat_min=col('seat_min'),
                          seat_max=col('seat_max'),
                          importstep=importstep)
        )
        result.append(room)
        if created:
            newN += 1
        else:
            existingN += 1
            room.importstep = importstep
            room.save()
    return (result, newN, existingN)


def _find_or_create_seats(rooms: tg.Sequence[arm.Room]) \
        -> tg.Tuple[tg.Sequence[arm.Seat], int, int]:
    result = []
    newN = existingN = 0
    for room in rooms:
        for seatnum in range(room.seat_min, room.seat_max + 1):
            seat, created = arm.Seat.objects.get_or_create(
                number=seatnum,
                room=room,
                defaults=dict(hash=arm.Seat.seathash(room, seatnum))
            )
            result.append(seat)
            if created:
                newN += 1
            else:
                existingN += 1
    return (result, newN, existingN)


def _excelerror(row: int = None, column: str = None,
                expected: str = None, found: str = None
                ):
    assert found  # the core part, must not be left empty
    result = "Excel error: "
    if row:
        result += f"row {row}  "
    if column:
        result += f"column '{column}'"
    if row or column:
        result += '\n'
    if expected:
        result += f"EXPECTED: {expected}\n"
    if expected:
        result += "FOUND: "
    result += found
    raise InvalidExcelError(result)


VGroupRow = collections.namedtuple('VGroupRow',  # noqa
    'familyname givenname email phone street_and_nr zipcode town '
    'cookie '
    'when from_time to_time '
    'organization department building room seat ')


Visits = tg.List[tg.Optional[arm.Visit]]


Expl = collections.namedtuple('Expl', ("Erklaerung", ))


explanations = [
    Expl("Jede Zeile im Tabellenblatt 'Daten' ist ein Besuch einer Person."),
    Expl("Die Spalten beschreiben die Person, dann die Zeit, dann den Ort."),
    Expl("Leere Zeilen trennen Gruppen von Personen, die sich "
         "laut ihren Angaben mindestens "
         f"{settings.MIN_OVERLAP_MINUTES} Minuten lang begegnet sind."),
    Expl(""),
    Expl("**cookie**: Die Daten enthalten meist viele Personen mehrfach."),
    Expl("Um diese Duplikate zu überblicken, kann man nach Spalte "
         "'cookie' sortieren."),
    Expl("Das selbe Cookie steht meist für die selbe Person."),
    Expl("Eine Person hat mehrere verschiedene Cookies, wenn sie "
         "das Cookie gelöscht oder mehrere Geräte oder Browser benutzt hat."),
    Expl("Zwei Personen können das selbe Cookie haben, wenn eine Person "
         "ihr Gerät verleiht."),
    Expl("Wenn viele Personen mit fragwürdigen Angaben das selbe Cookie "
         "haben, sind die Daten vielleicht Fantasie-Angaben."),
    Expl(""),
    Expl("**when**: Spalte 'when' enthält den Zeitpunkt der Meldung und "),
    Expl("'from' und 'to' die vom Besucher manuell eingegebenen Werte "
         "für den Zeitraum."),
    Expl(""),
    Expl(""),
    Expl(""),
    Expl(""),
]


def collect_visitgroups(primary_visits: djdmq.QuerySet
                        ) -> Visits:
    result = []
    visit_pks_seen = set()  # all contacts of primary visits
    primary_visit_pks_seen = set()  # only primary visits
    for pvisit in primary_visits:
        primary_visit_pks_seen.add(pvisit.pk)
        visit_pks_seen.add(pvisit.pk)
        group = pvisit.get_overlapping_visits()
        for visit in group:
            must_not_be_suppressed = (visit.pk == pvisit.pk)
            is_new = visit.pk not in visit_pks_seen
            if is_new or must_not_be_suppressed:
                result.append(visit)
        result.append(None)  # empty row as separator
    del result[-1]  # remove trailing empty row
    return result


def get_excel_download(visits: Visits) -> bytes:
    rows = _as_vgrouprows(visits)
    rowslists = dict(Daten=rows, Erklaerungen=explanations)
    with tempfile.NamedTemporaryFile(prefix="export_", suffix=".xlsx",
                                     delete=False) as fh:
        filename = fh.name  # file is deleted in 'finally' clause
    try:
        aue.write_excel_from_rowslists(filename, rowslists,  # type: ignore
                                       indexcolumn=True)
        with open(filename, 'rb') as file:
            excelbytes = file.read()  # slurp. Won't be very large.
    finally:
        os.unlink(filename)
    return excelbytes


def _as_vgrouprows(visits) -> tg.List[tg.Optional[VGroupRow]]:
    vgrouprows: tg.List[tg.Optional[VGroupRow]] = []
    v: arm.Visit
    for idx, v in enumerate(visits):
        if v is None:
            row = None
        else:
            row = VGroupRow(
                v.familyname, v.givenname, v.email, v.phone,
                v.street_and_number, v.zipcode, v.town, v.cookie,
                aud.dtstring(v.submission_dt, time=True), 
                aud.dtstring(v.present_from_dt, date=False, time=True),
                aud.dtstring(v.present_to_dt, date=False, time=True),
                v.seat.room.organization, v.seat.room.department, 
                v.seat.room.building, v.seat.room.room,
                v.seat.number)
        vgrouprows.append(row)
    return vgrouprows
