"""
# WebAPI文档

` python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml -n default -db mysql://user:pass@host/dbname `

## 执行参数:
    `-a`  - 绑定地址, 默认 "127.0.0.1"
    `-p`  - 绑定端口, 默认 9880
    `-c`  - TTS 配置文件路径, 默认 "GPT_SoVITS/configs/tts_infer.yaml"
    `-n`  - 当前 TTS 模型名称, 默认为当前文件夹名称
    `-db` - MySQL 数据库连接字符串, 格式 mysql://user:password@host/dbname，默认 "mysql://user:password@localhost/gpt_sovits"


## 调用:

### 推理

endpoint: `/tts`
GET:
```
http://127.0.0.1:9880/tts?text=先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。&text_lang=zh&ref_audio_path=archive_jingyuan_1.wav&prompt_lang=zh&prompt_text=我是「罗浮」云骑将军景元。不必拘谨，「将军」只是一时的身份，你称呼我景元便可&text_split_method=cut5&batch_size=1&media_type=wav&streaming_mode=true
```

POST:
```json
{
    "text": "",                   # str.(required) text to be synthesized
    "text_lang: "",               # str.(required) language of the text to be synthesized
    "ref_audio_path": "",         # str.(required) reference audio path
    "aux_ref_audio_paths": [],    # list.(optional) auxiliary reference audio paths for multi-speaker tone fusion
    "prompt_text": "",            # str.(optional) prompt text for the reference audio
    "prompt_lang": "",            # str.(required) language of the prompt text for the reference audio
    "top_k": 5,                   # int. top k sampling
    "top_p": 1,                   # float. top p sampling
    "temperature": 1,             # float. temperature for sampling
    "text_split_method": "cut0",  # str. text split method, see text_segmentation_method.py for details.
    "batch_size": 1,              # int. batch size for inference
    "batch_threshold": 0.75,      # float. threshold for batch splitting.
    "split_bucket: True,          # bool. whether to split the batch into multiple buckets.
    "speed_factor":1.0,           # float. control the speed of the synthesized audio.
    "streaming_mode": False,      # bool. whether to return a streaming response.
    "seed": -1,                   # int. random seed for reproducibility.
    "parallel_infer": True,       # bool. whether to use parallel inference.
    "repetition_penalty": 1.35    # float. repetition penalty for T2S model.
    "sample_steps": 32,           # int. number of sampling steps for VITS model V3.
    "super_sampling": False,       # bool. whether to use super-sampling for audio when using VITS model V3.
}
```

RESP:
成功: 直接返回 wav 音频流， http code 200
失败: 返回包含错误信息的 json, http code 400

### 命令控制

endpoint: `/control`

command:
"restart": 重新运行
"exit": 结束运行

GET:
```
http://127.0.0.1:9880/control?command=restart
```
POST:
```json
{
    "command": "restart"
}
```

RESP: 无


### 切换GPT模型

endpoint: `/set_gpt_weights`

GET:
```
http://127.0.0.1:9880/set_gpt_weights?weights_path=GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt
```
RESP:
成功: 返回"success", http code 200
失败: 返回包含错误信息的 json, http code 400


### 切换Sovits模型

endpoint: `/set_sovits_weights`

GET:
```
http://127.0.0.1:9880/set_sovits_weights?weights_path=GPT_SoVITS/pretrained_models/s2G488k.pth
```

RESP:
成功: 返回"success", http code 200
失败: 返回包含错误信息的 json, http code 400

### HealthCheck

endpoint: `/alive`

GET:
```
http://127.0.0.1:9880/alive
```

RESP:
成功: 返回"success", http code 200


"""

import os
import sys
import traceback
from typing import Generator
import pymysql
import datetime
from fastapi import Request
import yaml
import glob

now_dir = os.getcwd()
sys.path.append(now_dir)
sys.path.append("%s/GPT_SoVITS" % (now_dir))

import argparse
import subprocess
import wave
import signal
import numpy as np
import soundfile as sf
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn
from io import BytesIO
from tools.i18n.i18n import I18nAuto
from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config
from GPT_SoVITS.TTS_infer_pack.text_segmentation_method import get_method_names as get_cut_method_names
from pydantic import BaseModel

# print(sys.path)
i18n = I18nAuto()
cut_method_names = get_cut_method_names()

# 获取当前文件夹名称作为默认的模型名称
current_folder_name = os.path.basename(os.getcwd())

# 自动更新配置文件中的模型路径
def update_model_paths_in_config(config_path, model_name):
    if not os.path.exists(config_path):
        print(f"配置文件 {config_path} 不存在，将创建默认配置")
        # 让 TTS_Config 稍后自动创建默认配置
        return False
    
    try:
        with open(config_path, 'r', encoding='utf-8') as file:
            config = yaml.safe_load(file)
        
        if config is None:
            config = {}
            
        # 检查配置文件中是否有custom节点
        if 'custom' not in config:
            config['custom'] = {}
            # 如果没有custom节点，尝试复制default配置
            if 'default' in config:
                config['custom'] = config['default'].copy()
        
        # 保存原始版本信息
        original_version = config['custom'].get('version', 'v1')
        
        # 查找可能的模型文件
        model_dir = "GPT_SoVITS/pretrained_models"
        
        # 搜索模式，从特定到通用
        t2s_patterns = [
            f"{model_name}.ckpt",
            f"{model_name}_*.ckpt",
            f"{model_name}*.ckpt",
            f"*{model_name}*.ckpt",
            f"s1*{model_name}*.ckpt"
        ]
        
        sovits_patterns = [
            f"{model_name}.pth",
            f"{model_name}_*.pth",
            f"{model_name}*.pth",
            f"*{model_name}*.pth",
            f"s2*{model_name}*.pth"
        ]
        
        # 优先检查专用模型目录，然后再检查其他目录
        t2s_search_dirs = [
            os.path.join(now_dir, "GPT_weights_v2"),
            now_dir,
            os.path.join(now_dir, model_dir)
        ]
        
        sovits_search_dirs = [
            os.path.join(now_dir, "SoVITS_weights_v2"),
            now_dir, 
            os.path.join(now_dir, model_dir)
        ]
        
        t2s_path = None
        sovits_path = None
        
        # 搜索T2S模型文件
        for directory in t2s_search_dirs:
            if os.path.exists(directory):
                for pattern in t2s_patterns:
                    matches = glob.glob(os.path.join(directory, pattern))
                    if matches:
                        t2s_path = matches[0]
                        # 转换为相对路径
                        if t2s_path.startswith(now_dir):
                            t2s_path = os.path.relpath(t2s_path, now_dir)
                        print(f"在 {directory} 目录中找到T2S模型: {t2s_path}")
                        break
            if t2s_path:
                break
                
        # 搜索SoVITS模型文件
        for directory in sovits_search_dirs:
            if os.path.exists(directory):
                for pattern in sovits_patterns:
                    matches = glob.glob(os.path.join(directory, pattern))
                    if matches:
                        sovits_path = matches[0]
                        # 转换为相对路径
                        if sovits_path.startswith(now_dir):
                            sovits_path = os.path.relpath(sovits_path, now_dir)
                        print(f"在 {directory} 目录中找到SoVITS模型: {sovits_path}")
                        break
            if sovits_path:
                break
        
        # 自动检测模型版本
        detected_version = original_version
        if sovits_path:
            # 使用简单的启发式方法检测模型版本
            if "v3" in sovits_path.lower():
                detected_version = "v3"
            elif "v2" in sovits_path.lower():
                detected_version = "v2"
            elif "G488" in sovits_path:  # 默认模型标志
                detected_version = "v1"
        
        # 如果找到对应的模型文件，更新配置
        updated = False
        if t2s_path:
            config['custom']['t2s_weights_path'] = t2s_path
            print(f"已自动设置T2S模型路径: {t2s_path}")
            updated = True
        
        if sovits_path:
            config['custom']['vits_weights_path'] = sovits_path
            print(f"已自动设置SoVITS模型路径: {sovits_path}")
            updated = True
        
        # 更新版本信息
        if detected_version != original_version:
            config['custom']['version'] = detected_version
            print(f"已自动更新模型版本: {detected_version}")
            updated = True
        
        # 保存更新后的配置
        if updated:
            with open(config_path, 'w', encoding='utf-8') as file:
                yaml.dump(config, file, allow_unicode=True)
            return True
        
        return False
    except Exception as e:
        print(f"更新配置文件失败: {str(e)}")
        traceback.print_exc()
        return False

parser = argparse.ArgumentParser(description="GPT-SoVITS api")
parser.add_argument("-c", "--tts_config", type=str, default="GPT_SoVITS/configs/tts_infer.yaml", help="tts_infer路径")
parser.add_argument("-a", "--bind_addr", type=str, default="127.0.0.1", help="default: 127.0.0.1")
parser.add_argument("-p", "--port", type=int, default="9880", help="default: 9880")
parser.add_argument("-n", "--model_name", type=str, default=current_folder_name, help="当前TTS模型名称，默认为当前文件夹名称")
parser.add_argument("-db", "--db_config", type=str, default="mysql://user:password@localhost/gpt_sovits", 
                   help="MySQL数据库连接字符串，格式: mysql://user:password@host/dbname")
args = parser.parse_args()
config_path = args.tts_config
# device = args.device
port = args.port
host = args.bind_addr
model_name = args.model_name
db_config = args.db_config
argv = sys.argv

# 在加载TTS模型前尝试更新配置文件
if model_name != "default":
    print(f"正在尝试自动检测并配置模型: {model_name}")
    updated = update_model_paths_in_config(config_path, model_name)
    if updated:
        print(f"成功更新模型配置，模型名称: '{model_name}'")
    else:
        print(f"未找到与模型名称 '{model_name}' 匹配的模型文件，将使用配置文件中的默认路径")

# 解析数据库连接字符串
def parse_db_url(db_url):
    if not db_url.startswith("mysql://"):
        return None
    
    db_url = db_url.replace("mysql://", "")
    auth, rest = db_url.split("@", 1)
    user_pass = auth.split(":", 1)
    user = user_pass[0]
    password = user_pass[1] if len(user_pass) > 1 else ""
    
    host_db = rest.split("/", 1)
    host = host_db[0]
    dbname = host_db[1] if len(host_db) > 1 else ""
    
    return {
        "user": user,
        "password": password,
        "host": host,
        "database": dbname
    }

# 数据库连接参数
db_params = parse_db_url(db_config)

# 数据库记录函数
def record_tts_request(text, client_ip):
    if not db_params:
        print("数据库配置无效，跳过记录")
        return
    
    try:
        conn = pymysql.connect(
            host=db_params["host"],
            user=db_params["user"],
            password=db_params["password"],
            database=db_params["database"],
            charset='utf8mb4'  # 明确指定UTF-8编码
        )
        
        with conn.cursor() as cursor:
            # 检查并修改数据库字符集
            cursor.execute("ALTER DATABASE `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" % db_params["database"])
            
            # 检查表是否存在
            cursor.execute("SHOW TABLES LIKE 'tts_requests'")
            if cursor.fetchone():
                # 表存在，修改表的字符集
                cursor.execute("ALTER TABLE tts_requests CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
                cursor.execute("ALTER TABLE tts_requests MODIFY text TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            else:
                # 创建表
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS tts_requests (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    text TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
                    client_ip VARCHAR(45) NOT NULL,
                    model_name VARCHAR(255) NOT NULL,
                    request_time DATETIME NOT NULL
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                """)
            
            # 插入记录
            now = datetime.datetime.now()
            cursor.execute(
                "INSERT INTO tts_requests (text, client_ip, model_name, request_time) VALUES (%s, %s, %s, %s)",
                (text, client_ip, model_name, now)
            )
            
            conn.commit()
        
        conn.close()
        print(f"已记录TTS请求: {text[:30]}...")
    except Exception as e:
        print(f"记录TTS请求失败: {str(e)}")

if config_path in [None, ""]:
    config_path = "GPT-SoVITS/configs/tts_infer.yaml"

tts_config = TTS_Config(config_path)
print(tts_config)
tts_pipeline = TTS(tts_config)

APP = FastAPI()


class TTS_Request(BaseModel):
    text: str = None
    text_lang: str = None
    ref_audio_path: str = None
    aux_ref_audio_paths: list = None
    prompt_lang: str = None
    prompt_text: str = ""
    top_k: int = 5
    top_p: float = 1
    temperature: float = 1
    text_split_method: str = "cut5"
    batch_size: int = 1
    batch_threshold: float = 0.75
    split_bucket: bool = True
    speed_factor: float = 1.0
    fragment_interval: float = 0.3
    seed: int = -1
    media_type: str = "wav"
    streaming_mode: bool = False
    parallel_infer: bool = True
    repetition_penalty: float = 1.35
    sample_steps: int = 32
    super_sampling: bool = False


### modify from https://github.com/RVC-Boss/GPT-SoVITS/pull/894/files
def pack_ogg(io_buffer: BytesIO, data: np.ndarray, rate: int):
    with sf.SoundFile(io_buffer, mode="w", samplerate=rate, channels=1, format="ogg") as audio_file:
        audio_file.write(data)
    return io_buffer


def pack_raw(io_buffer: BytesIO, data: np.ndarray, rate: int):
    io_buffer.write(data.tobytes())
    return io_buffer


def pack_wav(io_buffer: BytesIO, data: np.ndarray, rate: int):
    io_buffer = BytesIO()
    sf.write(io_buffer, data, rate, format="wav")
    return io_buffer


def pack_aac(io_buffer: BytesIO, data: np.ndarray, rate: int):
    process = subprocess.Popen(
        [
            "ffmpeg",
            "-f",
            "s16le",  # 输入16位有符号小端整数PCM
            "-ar",
            str(rate),  # 设置采样率
            "-ac",
            "1",  # 单声道
            "-i",
            "pipe:0",  # 从管道读取输入
            "-c:a",
            "aac",  # 音频编码器为AAC
            "-b:a",
            "192k",  # 比特率
            "-vn",  # 不包含视频
            "-f",
            "adts",  # 输出AAC数据流格式
            "pipe:1",  # 将输出写入管道
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, _ = process.communicate(input=data.tobytes())
    io_buffer.write(out)
    return io_buffer


def pack_audio(io_buffer: BytesIO, data: np.ndarray, rate: int, media_type: str):
    if media_type == "ogg":
        io_buffer = pack_ogg(io_buffer, data, rate)
    elif media_type == "aac":
        io_buffer = pack_aac(io_buffer, data, rate)
    elif media_type == "wav":
        io_buffer = pack_wav(io_buffer, data, rate)
    else:
        io_buffer = pack_raw(io_buffer, data, rate)
    io_buffer.seek(0)
    return io_buffer


# from https://huggingface.co/spaces/coqui/voice-chat-with-mistral/blob/main/app.py
def wave_header_chunk(frame_input=b"", channels=1, sample_width=2, sample_rate=32000):
    # This will create a wave header then append the frame input
    # It should be first on a streaming wav file
    # Other frames better should not have it (else you will hear some artifacts each chunk start)
    wav_buf = BytesIO()
    with wave.open(wav_buf, "wb") as vfout:
        vfout.setnchannels(channels)
        vfout.setsampwidth(sample_width)
        vfout.setframerate(sample_rate)
        vfout.writeframes(frame_input)

    wav_buf.seek(0)
    return wav_buf.read()


def handle_control(command: str):
    if command == "restart":
        os.execl(sys.executable, sys.executable, *argv)
    elif command == "exit":
        os.kill(os.getpid(), signal.SIGTERM)
        exit(0)


def check_params(req: dict):
    text: str = req.get("text", "")
    text_lang: str = req.get("text_lang", "")
    ref_audio_path: str = req.get("ref_audio_path", "")
    streaming_mode: bool = req.get("streaming_mode", False)
    media_type: str = req.get("media_type", "wav")
    prompt_lang: str = req.get("prompt_lang", "")
    text_split_method: str = req.get("text_split_method", "cut5")

    if ref_audio_path in [None, ""]:
        return JSONResponse(status_code=400, content={"message": "ref_audio_path is required"})
    if text in [None, ""]:
        return JSONResponse(status_code=400, content={"message": "text is required"})
    if text_lang in [None, ""]:
        return JSONResponse(status_code=400, content={"message": "text_lang is required"})
    elif text_lang.lower() not in tts_config.languages:
        return JSONResponse(
            status_code=400,
            content={"message": f"text_lang: {text_lang} is not supported in version {tts_config.version}"},
        )
    if prompt_lang in [None, ""]:
        return JSONResponse(status_code=400, content={"message": "prompt_lang is required"})
    elif prompt_lang.lower() not in tts_config.languages:
        return JSONResponse(
            status_code=400,
            content={"message": f"prompt_lang: {prompt_lang} is not supported in version {tts_config.version}"},
        )
    if media_type not in ["wav", "raw", "ogg", "aac"]:
        return JSONResponse(status_code=400, content={"message": f"media_type: {media_type} is not supported"})
    elif media_type == "ogg" and not streaming_mode:
        return JSONResponse(status_code=400, content={"message": "ogg format is not supported in non-streaming mode"})

    if text_split_method not in cut_method_names:
        return JSONResponse(
            status_code=400, content={"message": f"text_split_method:{text_split_method} is not supported"}
        )

    return None


async def tts_handle(req: dict, request: Request = None):
    """
    Text to speech handler.

    Args:
        req (dict):
            {
                "text": "",                   # str.(required) text to be synthesized
                "text_lang: "",               # str.(required) language of the text to be synthesized
                "ref_audio_path": "",         # str.(required) reference audio path
                "aux_ref_audio_paths": [],    # list.(optional) auxiliary reference audio paths for multi-speaker synthesis
                "prompt_text": "",            # str.(optional) prompt text for the reference audio
                "prompt_lang: "",             # str.(required) language of the prompt text for the reference audio
                "top_k": 5,                   # int. top k sampling
                "top_p": 1,                   # float. top p sampling
                "temperature": 1,             # float. temperature for sampling
                "text_split_method": "cut5",  # str. text split method, see text_segmentation_method.py for details.
                "batch_size": 1,              # int. batch size for inference
                "batch_threshold": 0.75,      # float. threshold for batch splitting.
                "split_bucket: True,          # bool. whether to split the batch into multiple buckets.
                "speed_factor":1.0,           # float. control the speed of the synthesized audio.
                "fragment_interval":0.3,      # float. to control the interval of the audio fragment.
                "seed": -1,                   # int. random seed for reproducibility.
                "media_type": "wav",          # str. media type of the output audio, support "wav", "raw", "ogg", "aac".
                "streaming_mode": False,      # bool. whether to return a streaming response.
                "parallel_infer": True,       # bool.(optional) whether to use parallel inference.
                "repetition_penalty": 1.35    # float.(optional) repetition penalty for T2S model.
                "sample_steps": 32,           # int. number of sampling steps for VITS model V3.
                "super_sampling": False,       # bool. whether to use super-sampling for audio when using VITS model V3.
            }
    returns:
        StreamingResponse: audio stream response.
    """
    # 获取客户端IP
    client_ip = "unknown"
    if request:
        client_ip = request.client.host
        # 如果是通过代理，尝试获取原始IP
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()
    
    # 记录请求到数据库
    text = req.get("text", "")
    record_tts_request(text, client_ip)

    streaming_mode = req.get("streaming_mode", False)
    return_fragment = req.get("return_fragment", False)
    media_type = req.get("media_type", "wav")

    check_res = check_params(req)
    if check_res is not None:
        return check_res

    if streaming_mode or return_fragment:
        req["return_fragment"] = True

    try:
        tts_generator = tts_pipeline.run(req)

        if streaming_mode:

            def streaming_generator(tts_generator: Generator, media_type: str):
                if_frist_chunk = True
                for sr, chunk in tts_generator:
                    if if_frist_chunk and media_type == "wav":
                        yield wave_header_chunk(sample_rate=sr)
                        media_type = "raw"
                        if_frist_chunk = False
                    yield pack_audio(BytesIO(), chunk, sr, media_type).getvalue()

            # _media_type = f"audio/{media_type}" if not (streaming_mode and media_type in ["wav", "raw"]) else f"audio/x-{media_type}"
            return StreamingResponse(
                streaming_generator(
                    tts_generator,
                    media_type,
                ),
                media_type=f"audio/{media_type}",
            )

        else:
            sr, audio_data = next(tts_generator)
            audio_data = pack_audio(BytesIO(), audio_data, sr, media_type).getvalue()
            return Response(audio_data, media_type=f"audio/{media_type}")
    except Exception as e:
        return JSONResponse(status_code=400, content={"message": "tts failed", "Exception": str(e)})


@APP.get("/control")
async def control(command: str = None):
    return JSONResponse(status_code=403, content={"message": "此功能已被禁用"})


@APP.get("/tts")
async def tts_get_endpoint(
    request: Request,
    text: str = None,
    text_lang: str = None,
    ref_audio_path: str = None,
    aux_ref_audio_paths: list = None,
    prompt_lang: str = None,
    prompt_text: str = "",
    top_k: int = 5,
    top_p: float = 1,
    temperature: float = 1,
    text_split_method: str = "cut0",
    batch_size: int = 1,
    batch_threshold: float = 0.75,
    split_bucket: bool = True,
    speed_factor: float = 1.0,
    fragment_interval: float = 0.3,
    seed: int = -1,
    media_type: str = "wav",
    streaming_mode: bool = False,
    parallel_infer: bool = True,
    repetition_penalty: float = 1.35,
    sample_steps: int = 32,
    super_sampling: bool = False
):
    req = {
        "text": text,
        "text_lang": text_lang.lower(),
        "ref_audio_path": ref_audio_path,
        "aux_ref_audio_paths": aux_ref_audio_paths,
        "prompt_text": prompt_text,
        "prompt_lang": prompt_lang.lower(),
        "top_k": top_k,
        "top_p": top_p,
        "temperature": temperature,
        "text_split_method": text_split_method,
        "batch_size": int(batch_size),
        "batch_threshold": float(batch_threshold),
        "speed_factor": float(speed_factor),
        "split_bucket": split_bucket,
        "fragment_interval": fragment_interval,
        "seed": seed,
        "media_type": media_type,
        "streaming_mode": streaming_mode,
        "parallel_infer": parallel_infer,
        "repetition_penalty": float(repetition_penalty),
        "sample_steps": int(sample_steps),
        "super_sampling": super_sampling,
    }
    return await tts_handle(req, request)


@APP.post("/tts")
async def tts_post_endpoint(request: TTS_Request, req: Request):
    req_dict = request.dict()
    return await tts_handle(req_dict, req)


@APP.get("/set_refer_audio")
async def set_refer_aduio(refer_audio_path: str = None):
    return JSONResponse(status_code=403, content={"message": "此功能已被禁用"})


@APP.get("/set_gpt_weights")
async def set_gpt_weights(weights_path: str = None):
    return JSONResponse(status_code=403, content={"message": "此功能已被禁用"})


@APP.get("/set_sovits_weights")
async def set_sovits_weights(weights_path: str = None):
    return JSONResponse(status_code=403, content={"message": "此功能已被禁用"})

@APP.get("/alive")
async def alive():
    return Response(status_code=200, content="success")

if __name__ == "__main__":
    try:
        if host == "None":  # 在调用时使用 -a None 参数，可以让api监听双栈
            host = None
        uvicorn.run(app=APP, host=host, port=port, workers=1)
    except Exception:
        traceback.print_exc()
        os.kill(os.getpid(), signal.SIGTERM)
        exit(0)
