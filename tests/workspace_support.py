"""Pinned acceptance inventory independent of route registrations."""
POST_SUFFIXES = frozenset({
    '/pis', '/pis/{user_id}/verify-email', '/pis/{user_id}/profile',
    '/pis/{user_id}/profile/retry', '/pis/{user_id}/candidates/{candidate_id}/accept',
    '/pis/{user_id}/candidates/{candidate_id}/reject', '/pis/{user_id}/publications/{publication_id}/keep',
    '/pis/{user_id}/publications/{publication_id}/exclude', '/pis/{user_id}/publications/{publication_id}/restore',
    '/pis/{user_id}/draft/accept', '/pis/{user_id}/draft/discard', '/pis/{user_id}/regenerate',
    '/pis/{user_id}/persona/reexport', '/pis/{user_id}/grants/{grant_id}/veto',
    '/pis/{user_id}/grant-identity/pin', '/pis/{user_id}/grant-identity/none',
    '/pis/{user_id}/grant-identity/unpin', '/pis/{user_id}/orcid-fundings/{funding_id}/veto',
    '/pis/{user_id}/industry/{evidence_id}/veto', '/pis/{user_id}/companies',
    '/pis/{user_id}/companies/discover', '/pis/{user_id}/companies/{company_id}/delete',
    '/pis/{user_id}/companies/{company_id}/confirm', '/pis/{user_id}/companies/{company_id}/reject',
    '/pis/{user_id}/mute', '/pis/{user_id}/unmute', '/pis/{user_id}/slack/provision',
    '/pis/{user_id}/activate',
})
