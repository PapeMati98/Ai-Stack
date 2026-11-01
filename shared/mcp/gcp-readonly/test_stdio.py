import io
import json
import unittest
import server


class StdioTests(unittest.TestCase):
    def test_standard_ndjson_unicode_and_roundtrip(self):
        message={'jsonrpc':'2.0','id':1,'method':'ping','params':{'text':'ąę'}}
        stream=io.BytesIO()
        server.write_message(stream,message)
        raw=stream.getvalue()
        self.assertEqual(len(raw.splitlines()),1)
        self.assertTrue(raw.endswith(b'\n'))
        self.assertEqual(json.loads(raw),message)
        self.assertEqual(server.read_message(io.BytesIO(raw)),message)
    def test_bounded_read_and_invalid_inputs(self):
        for raw in [b'x'*1025+b'\n',b'{"id":1,"id":2}\n',b'{}',b'Content-Length: 2\r\n\r\n{}',b'[]\n']:
            with self.assertRaises(server.SafeToolError):
                server.read_message(io.BytesIO(raw),max_bytes=1024)


if __name__=='__main__':
    unittest.main()
