import json
import os
import shutil
import uuid

from flask import Flask, request, Response
from raven.contrib.flask import Sentry

from compiler import Compiler
from config import JUDGER_WORKSPACE_BASE, SPJ_SRC_DIR, SPJ_EXE_DIR, COMPILER_GROUP_GID
from exception import TokenVerificationFailed, CompileError, SPJCompileError, JudgeClientError
from judge_client import JudgeClient
from utils import server_info, logger, token


app = Flask(__name__)
sentry = Sentry(app, dsn='')
DEBUG = os.environ.get("judger_debug") == "1"
app.debug = DEBUG


class InitSubmissionEnv(object):
    def __init__(self, judger_workspace, submission_id):
        self.path = os.path.join(judger_workspace, submission_id)

    def __enter__(self):
        try:
            os.mkdir(self.path)
            os.chown(self.path, 0, COMPILER_GROUP_GID)
            os.chmod(self.path, 0o771)
        except Exception as e:
            logger.exception(e)
            raise JudgeClientError("failed to create runtime dir")
        return self.path

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not DEBUG:
            try:
                shutil.rmtree(self.path)
            except Exception as e:
                logger.exception(e)
                raise JudgeClientError("failed to clean runtime dir")


class JudgeServer:
    @classmethod
    def ping(cls):
        data = server_info()
        data["action"] = "pong"
        return data

    @classmethod
    def judge(cls, language_config, src, max_cpu_time, max_memory, test_case_id,
              spj_version=None, spj_config=None, spj_compile_config=None, spj_src=None, output=False):
        # init
        compile_config = language_config.get("compile")
        run_config = language_config["run"]
        submission_id = uuid.uuid4().hex

        if spj_version and spj_config:
            spj_exe_path = os.path.join(SPJ_EXE_DIR, spj_config["exe_name"].format(
                spj_version=spj_version,
                test_case_id=test_case_id
            ))
            # spj src has not been compiled
            if not os.path.isfile(spj_exe_path):
                logger.warning("%s does not exists, spj src will be recompiled")
                cls.compile_spj(spj_version=spj_version, src=spj_src,
                                spj_compile_config=spj_compile_config,
                                test_case_id=test_case_id)

        with InitSubmissionEnv(JUDGER_WORKSPACE_BASE, submission_id=str(submission_id)) as submission_dir:
            if compile_config:
                src_path = os.path.join(submission_dir, compile_config["src_name"])

                # write source code into file
                with open(src_path, "w", encoding="utf-8") as f:
                    f.write(src)

                # compile source code, return exe file path
                exe_path = Compiler().compile(compile_config=compile_config,
                                              src_path=src_path,
                                              output_dir=submission_dir)
            else:
                exe_path = os.path.join(submission_dir, run_config["exe_name"])
                with open(exe_path, "w", encoding="utf-8") as f:
                    f.write(src)

            judge_client = JudgeClient(run_config=language_config["run"],
                                       exe_path=exe_path,
                                       max_cpu_time=max_cpu_time,
                                       max_memory=max_memory,
                                       test_case_id=str(test_case_id),
                                       submission_dir=submission_dir,
                                       spj_version=spj_version,
                                       spj_config=spj_config,
                                       output=output)
            run_result = judge_client.run()

            return run_result

    @classmethod
    def compile_spj(cls, spj_version, src, spj_compile_config, test_case_id):
        spj_compile_config["src_name"] = spj_compile_config["src_name"].format(
            spj_version=spj_version,
            test_case_id=test_case_id
        )
        spj_compile_config["exe_name"] = spj_compile_config["exe_name"].format(
            spj_version=spj_version,
            test_case_id=test_case_id
        )

        spj_src_path = os.path.join(SPJ_SRC_DIR, spj_compile_config["src_name"])

        # if spj source code not found, then write it into file
        if not os.path.exists(spj_src_path):
            with open(spj_src_path, "w", encoding="utf-8") as f:
                f.write(src)
            os.chown(spj_src_path, 0, COMPILER_GROUP_GID)
            os.chmod(spj_src_path, 0o660)

        try:
            exe_path = Compiler().compile(compile_config=spj_compile_config,
                                          src_path=spj_src_path,
                                          output_dir=SPJ_EXE_DIR)
            os.chmod(exe_path, 0o771)
        # turn common CompileError into SPJCompileError
        except CompileError as e:
            raise SPJCompileError(e.message)
        return "success"


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>', methods=["POST"])
def server(path):
    if path in ("judge", "ping", "compile_spj"):
        _token = request.headers.get("X-Judge-Server-Token")
        try:
            if _token != token:
                raise TokenVerificationFailed("invalid token")
            try:
                data = request.json
            except Exception:
                data = {}
            ret = {"err": None, "data": getattr(JudgeServer, path)(**data)}
        except (CompileError, TokenVerificationFailed, SPJCompileError, JudgeClientError) as e:
            logger.exception(e)
            sentry.captureException()
            ret = {"err": e.__class__.__name__, "data": e.message}
        except Exception as e:
            logger.exception(e)
            sentry.captureException()
            ret = {"err": "JudgeClientError", "data": e.__class__.__name__ + " :" + str(e)}
    else:
        ret = {"err": "InvalidRequest", "data": "404"}
    return Response(json.dumps(ret), mimetype='application/json')


if DEBUG:
    logger.info("DEBUG=ON")

# gunicorn -w 4 -b 0.0.0.0:8080 server:app
if __name__ == "__main__":
    app.run(debug=DEBUG)
