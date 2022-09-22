from urllib.parse import urljoin

import requests


class APIError(IOError):
    pass


class XINGEventsAPIClient:
    base_url = 'https://www.xing-events.com/api/'

    def __init__(self, apikey):
        self.apikey = apikey

    def _headers(self):
        return {
            'Authorization': f'ApiKey {self.apikey}'
        }

    def _get(self, path, **kwargs):
        r = requests.get(
            urljoin(self.base_url, path),
            headers=self._headers(),
            **kwargs
        )
        r.raise_for_status()
        d = r.json()
        if not d['success']:
            raise APIError(f'API returned success=false for {path}')
        return d

    def get_event_ids(self):
        d = self._get('event/find')
        return d['ids']
