import test_claude_background_lifecycle as fixtures
from test_native_results import PROMPT, c_user


class CompactSummaryTests(fixtures.JobReaderTests):
    def test_transcript_only_summary_is_data_and_does_not_drop_pending_jobs(self):
        row=c_user(PROMPT,isCompactSummary=True,isVisibleInTranscriptOnly=True)
        self.assertEqual(self.check(self.rows+[row]).kind,'pending')
        self.assertEqual(self.check(self.rows+[fixtures.completion(),fixtures.c_final('finished'),row]).kind,'clear')
    def test_malformed_summary_flags_content_and_duplicate_real_prompt_refuse(self):
        for changes in ({'isCompactSummary':1},{'isCompactSummary':'true'},{'isCompactSummary':True},
                        {'isCompactSummary':True,'isVisibleInTranscriptOnly':True,'type':'assistant'},
                        {'isCompactSummary':True,'isVisibleInTranscriptOnly':True,'message':{'role':'user','content':[{'type':'tool_result'}]}}):
            with self.subTest(changes=changes):self.assertEqual(self.check(self.rows+[c_user(PROMPT,**changes)]).kind,'unknown')
        self.assertEqual(self.check(self.rows+[c_user(PROMPT)]).kind,'unknown')
