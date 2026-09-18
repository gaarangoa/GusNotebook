"""PDF tab classification, authenticated streaming, and viewer headers."""

from pathlib import Path
import tempfile
import unittest

from gusnotebook.app import create_app, close_app


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.pdf = self.root / 'report # & α.PDF'
        self.content = (Path(__file__).parent / 'fixtures/sample.pdf').read_bytes()
        self.pdf.write_bytes(self.content)
        self.app = create_app({'WORK_DIR': str(self.root), 'STATE_DIR': str(self.root / 'state'),
                               'START_WATCHERS': False, 'AUTH_TOKEN': 'test'})
        self.client = self.app.test_client()
        self.client.post('/auth', headers={'Authorization': 'Bearer test'})

    def tearDown(self):
        close_app(self.app)
        self.temp.cleanup()

    def test_open_restore_download_and_byte_ranges(self):
        opened = self.client.post('/api/open', json={'path': str(self.pdf)})
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.json['kind'], 'pdf')
        url = opened.json['url']
        with self.client.get(url) as response:
            self.assertEqual(response.data, self.content)
            self.assertEqual(response.mimetype, 'application/pdf')
            self.assertEqual(response.headers['X-Frame-Options'], 'SAMEORIGIN')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertTrue(response.headers['Content-Disposition'].startswith('inline'))
        with self.client.get(url, headers={'Range': 'bytes=0-7'}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, self.content[:8])
        self.assertIn({'path': str(self.pdf), 'kind': 'pdf'}, self.client.get('/api/tabs').json['tabs'])
        with self.client.get('/api/files/download', query_string={'path': str(self.pdf)}) as response:
            self.assertEqual(response.data, self.content)
            self.assertTrue(response.headers['Content-Disposition'].startswith('attachment'))

    def test_missing_files_auth_and_other_content_stay_protected(self):
        missing = str(self.root / 'missing.pdf')
        self.assertEqual(self.client.post('/api/open', json={'path': missing}).status_code, 404)
        self.assertEqual(self.client.get('/api/pdf', query_string={'path': missing}).status_code, 404)
        html = self.root / 'page.html'
        html.write_text('<h1>HTML</h1>')
        self.assertEqual(self.client.get('/api/pdf', query_string={'path': str(html)}).status_code, 400)
        self.assertEqual(self.app.test_client().get('/api/pdf', query_string={'path': str(self.pdf)}).status_code, 401)
        self.assertEqual(self.client.get('/').headers['X-Frame-Options'], 'DENY')
        with self.client.get('/api/raw', query_string={'path': str(html)}) as response:
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')


if __name__ == '__main__':
    unittest.main()
