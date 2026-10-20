import unittest
from pathlib import Path

import herdr_artifacts as ha


class ReportFilenameTests(unittest.TestCase):
    def test_authoritative_report_uses_task_directory(self):
        self.assertEqual(ha.report_filename('A long original request', Path('/reviews/source-warning-fix-20261020/FINAL_REPORT.md')), 'source-warning-fix-report.md')

    def test_title_stays_short_and_never_becomes_a_path(self):
        name = ha.report_filename('../../Source warning fix: final report', Path('/generated/random.md'))
        self.assertEqual(name, 'source-warning-fix-report.md')
        self.assertNotIn('/', name)
        self.assertLessEqual(len(ha.report_filename('long ' * 100, Path('/generated/file.md'))), 90)

    def test_turkish_title_is_preserved_and_empty_title_has_safe_fallback(self):
        self.assertEqual(ha.report_filename('Kaynak uyarısı düzeltmesi', Path('/generated/file.md')), 'kaynak-uyarısı-düzeltmesi-report.md')
        self.assertEqual(ha.report_filename('Final task report', Path('/generated/file.md')), 'completed-task-report.md')
