from typing import Tuple, Dict
import json
import os
import time
import datetime
import server.common as common
import requests
import math
from transcribe import WhisperModel

db_dir = common.getDbDir()
task_file_path = common.getTaskFilePath()
input_audio_dir = common.getInputDir()
worker_name = common.getWorkerName()
worker_idle_seconds = common.getBackgroundIdleSeconds()
kikoeru_url = common.getKikoeruUrl()
kikoeru_user = common.getKikoeruUser()
kikoeru_password = common.getKikoeruPassword()
transcribe_params = common.getTrancribeParams()
save_lrc_file = common.getSaveLrcFile()
is_need_auth = False
model: WhisperModel = None

# 使用session进行通信，保存token，每一次运行前检查token是否失效，如果失效，需要重新登陆验证
def setupSession(session:requests.Session, token:str):
    headers = {
        # "Content-Type": "application/json",
    }
    if token != "":
        headers["Authorization"] = f"Bearer {token}"
    session.headers = headers

session = requests.session()
setupSession(session, common.getToken())

# 检查kikoeru用户验证
# 返回False表示不需要用户验证
# 返回True表示需要用户验证
def checkKikoeruAuth(url):
    response = session.get(
        f"{url}/api/auth/me"
    )
    if response.status_code == 200:
        print("当前状态下kikoeru服务器可直接通信")
        return False
    elif response.status_code == 401:
        print("kikoeru服务器需要用户验证")
        return True
    else:
        print(response)
        raise Exception(f"检查服务器登陆时，发生未知错误：{response.status_code}")

def loginKikoeru(url:str, user:str, password:str)->str:
    print("尝试登陆获取token")
    response = session.post(
        f"{url}/api/auth/me",
        {
            "name": user,
            "password": password,
        }
    )
    if response.status_code == 200:
        print("登陆成功")
        kikoeru_token = response.json()['token']
        print("token = ", kikoeru_token)
        return kikoeru_token
    else:
        print("登陆失败", response)
        return ""

# return [success, not_task_can_acquire, task]
def acquireTask(url:str)->Tuple[bool, bool, Dict]:
    try:
        res = session.post(
            f"{url}/api/lyric/translate/acquire",
            {
                "worker_name": worker_name,
            }
        )
        # print("acquire task response: ", res.status_code)
        no_task_can_acquire = res.status_code == 404
        success = res.status_code == 200
        data = res.json()
    except:
        return [False, True, None]
    # print(data)
    return (success, no_task_can_acquire, data)

def sleepAndWait(secs:int, info):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\r{info} ({now}) wait for another {secs} seconds", end="")
    time.sleep(secs)

def updateTaskStatus(task:Dict, status:str)->bool:
    try:
        res = session.post(
            f"{kikoeru_url}/api/lyric/translate/status",
            {
                **task,
                "worker_status": status,
            }
        )
        print("  任务进度：", status)
        
        return res.json()['success']
    except Exception as e:
        print("updateTaskStatus error: ", e)

def downloadAudioFile(task:Dict, save_name:str)->bool:
    try:
        r = session.get(f"{kikoeru_url}/api/lyric/translate/download", params={
            "id": task["id"],
            "secret": task["secret"],
        }, stream=True)
        with open(os.path.join(input_audio_dir, save_name), 'wb') as fd:
            for chunk in r.iter_content(chunk_size=128):
                fd.write(chunk)
        return True
    except Exception as e:
        print("下载音频失败：", e)
        return False

def finishTask(task:Dict, success:bool, lrc_content:str):
    print("上传任务结果")
    try:
        r = session.post(f"{kikoeru_url}/api/lyric/translate/finish", json={
            "id": task["id"],
            "secret": task["secret"],
            "success": success,
            "lrc_content": lrc_content,
        })
        if r.status_code != 200:
            print("上传任务结果失败", r.json())
    except Exception as e:
        print("上传任务结果失败：", e)
        return False

def saveTaskToFile(task:Dict):
    with open(task_file_path, "w", encoding="utf8") as f:
        json.dump(task, f, indent=4)

def format_seconds(
    seconds: float,
    decimal_marker: str = ".",
) -> str:
    assert seconds >= 0, "non-negative timestamp expected"

    just_seconds = math.floor(seconds) % 60
    just_hundredths_seconds = math.floor(100 * (seconds - math.floor(seconds))) # lrc 秒数以下用0-99显示剩余数位，而不是毫秒，所以这里乘以100, ref: https://en.wikipedia.org/wiki/LRC_(file_format)
    just_minutes = math.floor(seconds / 60)
    return (
        f"[{just_minutes:02d}:{just_seconds:02d}{decimal_marker}{just_hundredths_seconds:02d}]"
    )

# output complete lrc contents joined by '\n'
def transcribe_audio(audio_path:str)->str:
    global model
    segments, _ = model.transcribe(audio=audio_path, **transcribe_params)
    return "\n".join(
        map(
            lambda segment: f"{format_seconds(segment.start)} {segment.text}",
            segments,
        )
    )

INVALID_FILENAME_CHARS = '<>:"/\\|?*'

# 音频文件名来自服务器上的真实文件，可能含有 Windows 不允许的字符
def sanitizeFileName(name:str)->str:
    for c in INVALID_FILENAME_CHARS:
        name = name.replace(c, "_")
    return name.strip().rstrip(".")  # Windows 下文件名结尾不能是空格或点

# 服务器用 RJ + 补零后的 work_id 表示音声编号，与 kikoeru-express 的 formatID 保持一致
def formatWorkCode(work_id:int)->str:
    n = int(work_id)
    if n >= 1000000:
        return "RJ" + ("0" + str(n))[-8:]
    return "RJ" + ("000000" + str(n))[-6:]

# 本地留存的字幕文件路径：OUTPUT_PATH/RJ号/音频文件名.lrc
# 服务器没有返回作品信息时（老版本服务端），退化成 OUTPUT_PATH/任务id.lrc
def buildLrcPath(task:Dict)->str:
    output_dir = common.getOutputDir()
    work_id = task.get("work_id")
    audio_stem = sanitizeFileName(os.path.splitext(os.path.basename(task.get("audio_path") or ""))[0])
    if not work_id or audio_stem == "":
        return os.path.join(output_dir, f"{task['id']}.lrc")
    return os.path.join(output_dir, formatWorkCode(work_id), f"{audio_stem}.lrc")

# 把转译结果额外写一份到本地磁盘，便于事后排查或者重复使用
def saveLrcFile(task:Dict, lrc_content:str):
    output_lrc_path = buildLrcPath(task)
    os.makedirs(os.path.dirname(output_lrc_path), exist_ok=True)
    with open(output_lrc_path, "w", encoding="utf8") as f:
        f.write(lrc_content)
    print("字幕文件已保存到：", output_lrc_path)

# 检查任务是否还归属于自己（没有被删除、没有被重新分配给别的worker），
# 返回服务器上的任务详情（含 work_id / audio_path），任务已失效时返回 None
def fetchOwnTaskDetail(task:Dict)->Dict:
    try:
        r = session.get(f"{kikoeru_url}/api/lyric/translate/get", params={
            "id": task["id"],
            "secret": task["secret"],
        })
        if r.status_code == 200:
            data = r.json()
            print("get task status, data = ", data)
            return data.get("task") or None
        else:
            return None
    except Exception as e:
        print("检查任务状态失败：", e)
        raise e

# task = {id: 0, secret: "777f7f7f77fd7"}
def processTask(task):
    print("存储task信息到本地文件中") # 目前一个worker一次只处理一个task
    # 当进行处理的时候，通过一个本地文件记录，方便重启翻译服务器后继续前面的任务
    saveTaskToFile(task)

    # 处理前，获取一次任务状态，如果任务被删除了的话，则不处理，直接返回
    detail = fetchOwnTaskDetail(task)
    if detail is None:
        print("服务器上的翻译任务已被删除，或者已经被重新启动翻译进程，跳过当前任务")
        os.unlink(task_file_path)

        if 'audio_file_name' in task:
            print("删除本地音频文件")
            os.unlink(os.path.join(input_audio_dir, task['audio_file_name']))
        return

    # 记下服务器侧的作品/音频信息，本地字幕文件按 RJ号/音频文件名.lrc 命名
    task['work_id'] = detail.get("work_id")
    task['audio_path'] = detail.get("audio_path")
    saveTaskToFile(task)

    if 'audio_file_name' not in task:
        print("下载音频文件")
        audio_file_name = f"{task['id']}{task['audio_ext']}"
        if not downloadAudioFile(task, audio_file_name):
            finishTask(task, False, "音频下载失败")
            os.unlink(task_file_path)
            return

        task['audio_file_name'] = audio_file_name
        saveTaskToFile(task)
    else:
        audio_file_name = task['audio_file_name']

    audio_file_path = os.path.join(input_audio_dir, audio_file_name)
    print("音频文件位于：", audio_file_path)

    success = False
    lrc_content = ""
    try:
        print("翻译中...")
        lrc_content = transcribe_audio(audio_file_path)
        success = True
        print("翻译成功")
    except Exception as e:
        print("transcripting error, ", e)
        success = False
        
    # 上传前先存一份本地副本，这样即使上传失败也不会丢掉转译结果
    if success and save_lrc_file:
        try:
            saveLrcFile(task, lrc_content)
        except Exception as e:
            print("保存本地字幕文件失败（不影响上传）：", e)

    finishTask(task, success, lrc_content)

    print(" 任务完成，删除本地记录")
    os.unlink(audio_file_path)
    os.unlink(task_file_path)

def clearOldTaskAtStartup():
    print("尝试处理上一次没有完成的翻译任务")
    if not os.path.exists(task_file_path):
        print("没有遗留的未完成任务，继续正常运行")
        return

    print("发现有未完成的任务，加载并执行")
    with open(task_file_path, "r", encoding="utf8") as f:
        task = json.load(f)
    processTask(task)

def load_model():
    print("load model start")
    global model
    model_path = common.getModelPath()
    device = common.getTranscribeDevice()
    compute_type = "default"

    try:
        model = WhisperModel(model_path, device=device, compute_type=compute_type)
        print("load model finished, start background loop, waiting for transcribe task")
    except Exception as e:
        print(f"Error loading model: {e}, program quit")
        exit(1)

def main():
    print("hello world: ")
    print("kikoeru_url = ", kikoeru_url)
    print("kikoeru_user = ", kikoeru_user)
    print("kikoeru_password = ", kikoeru_password)
    print("this translate worker name is: ", worker_name)
    print("whisper model transcribe params:", transcribe_params)
    
    global is_need_auth
    global kikoeru_token
    global model

    is_need_auth = checkKikoeruAuth(kikoeru_url)

    if is_need_auth:
        kikoeru_token = loginKikoeru(kikoeru_url, kikoeru_user, kikoeru_password)
        if kikoeru_token != "":
            common.saveToken(kikoeru_token)
            setupSession(session, kikoeru_token)

    load_model()

    clearOldTaskAtStartup()
    
    while True:
        success, run_out_of_task, task = acquireTask(kikoeru_url)

        if run_out_of_task:
            sleepAndWait(worker_idle_seconds, "翻译队列为空")
        elif not success:
            sleepAndWait(worker_idle_seconds, f"发生错误(${task})")
        else: # success
            print("")
            print("task.id = ", task['id'], "task.secret = ", task['secret'])
            processTask(task)
            
if __name__ == "__main__":
    main()