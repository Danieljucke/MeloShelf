"""HTTP-facing logic for artist enrichment jobs.

At most one job runs at a time, in one background thread. Starting needs a provider: the injected test client, or
the MusicBrainz client built from the saved public contact. Without either, the request is refused rather than
sending anything.
"""
import logging
import threading

from meloshelf.application.artist_enrichment import EnrichmentService, JobError
from meloshelf.infrastructure.music_api import MusicBrainzClient

log = logging.getLogger('meloshelf.enrichment')

SETTING = 'artist_enrichment_auto'   # opt-in; off unless the person turns it on
AUTO_LIMIT = 200                      # names per automatic run, so one scan never starts an unbounded lookup


from meloshelf.application import error_codes as ec
def _coded_fields(answer):
    return {'reason': answer['error'], 'code': answer['code'], 'params': answer['params']}


def _coded_start(text):
    return {'started': False, **_coded_fields(ec.from_text(text))}


def _unexpected_start(exc):
    log.exception('automatic enrichment could not start')
    return {'started': False, **_coded_fields(ec.unexpected(exc))}


def _client(server):
    if server.artist_client is not None:
        return server.artist_client
    agent = server.config.musicbrainz_user_agent()[0]
    return MusicBrainzClient(agent) if agent else None


def _worker_busy(server) -> bool:
    thread = getattr(server, 'enrichment_thread', None)
    return thread is not None and thread.is_alive()


def _launch(server, service, job_id):
    thread = threading.Thread(target=service.run_to_completion, args=(job_id,), daemon=True,
                              name=f'artist-enrichment-{job_id}')
    server.enrichment_thread = thread
    thread.start()


def start(server, payload: dict) -> tuple[int, dict]:
    max_names = payload.get('max_names', 500)
    if _worker_busy(server):
        return 409, ec.body('enrichment_job_running')
    client = _client(server)
    if client is None:
        return 409, ec.body('enrichment_contact_required')
    service = EnrichmentService(server.database, client)
    try:
        job = service.create(max_names=max_names)
    except JobError as exc:
        return 400, ec.from_text(exc)
    if job['planned'] == 0:
        return 200, {'id': job['id'], 'planned': 0, 'status': 'done', 'nothing_to_do': True}
    _launch(server, service, job['id'])
    return 202, {'id': job['id'], 'planned': job['planned'], 'status': 'queued'}


def control(server, payload: dict) -> tuple[int, dict]:
    job_id, action = payload.get('id'), payload.get('action')
    if type(job_id) is not int:
        return 400, ec.body('job_id_not_integer')
    client = _client(server)
    service = EnrichmentService(server.database, client)
    try:
        if action == 'pause':
            service.pause(job_id)
        elif action == 'cancel':
            service.cancel(job_id)
        elif action == 'resume':
            if _worker_busy(server):
                return 409, ec.body('enrichment_other_running')
            if client is None:
                return 409, ec.body('enrichment_contact_required_resume')
            service.resume(job_id)
            _launch(server, service, job_id)
        else:
            return 400, ec.body('enrichment_action_invalid')
    except JobError as exc:
        return 400, ec.from_text(exc)
    return 200, service.status(job_id)


def status(server, job_id_text: str | None) -> tuple[int, dict]:
    service = EnrichmentService(server.database, None)
    if job_id_text in (None, ''):
        with server.database.read() as db:
            row = db.execute('SELECT id FROM artist_enrichment_jobs ORDER BY id DESC LIMIT 1').fetchone()
        if row is None:
            return 200, {'status': 'none'}
        job_id = row['id']
    else:
        try:
            job_id = int(job_id_text)
        except ValueError:
            return 400, ec.body('job_id_not_integer')
    try:
        return 200, service.status(job_id)
    except JobError as exc:
        return 400, ec.from_text(exc)


def auto_enabled(server) -> bool:
    return server.store.get_setting(SETTING, False) is True


def after_scan(server) -> dict:
    """Runs after a completed scan. Does nothing unless the person enabled it, and never more than AUTO_LIMIT names.
    Refusals are returned, not raised: a scan that finished must stay finished."""
    try:
        if not auto_enabled(server):
            return _coded_start('Automatic artist enrichment is off.')
        if _worker_busy(server):
            return _coded_start('An enrichment job is already running. Pause or cancel it first.')
        client = _client(server)
        if client is None:
            return _coded_start('Save a MusicBrainz contact in Settings to enable automatic enrichment.')
        service = EnrichmentService(server.database, client)
        job = service.create(max_names=AUTO_LIMIT)
        if job['planned'] == 0:
            return _coded_start('Every artist name already has a stored answer.')
        _launch(server, service, job['id'])
        return {'started': True, 'id': job['id'], 'planned': job['planned']}
    except Exception as exc:  # logged, never raised into the scan
        log.exception('automatic enrichment could not start')
        return _unexpected_start(exc)


def get_settings(server) -> tuple[int, dict]:
    return 200, {'auto_enrich': auto_enabled(server), 'limit_per_scan': AUTO_LIMIT, 'remaining': remaining(server)}


def set_settings(server, payload: dict) -> tuple[int, dict]:
    value = payload.get('auto_enrich')
    if not isinstance(value, bool):
        return 400, ec.body('auto_enrich_not_boolean')
    server.store.set_setting(SETTING, value)
    return 200, {'auto_enrich': value, 'limit_per_scan': AUTO_LIMIT}


def remaining(server) -> int:
    """Distinct artist names that still have no stored answer: the work left for later batches. The batch limit
    (AUTO_LIMIT) only bounds one run; it never hides names, and every run continues with the ones not yet answered."""
    from meloshelf.application.artist_lookup import distinct_names
    from meloshelf.domain.artists import canonical_key
    from meloshelf.infrastructure.artist_identity_store import ArtistLinks
    stored = ArtistLinks(server.database).all()
    return sum(1 for name in distinct_names(server.database) if canonical_key(name) not in stored)
