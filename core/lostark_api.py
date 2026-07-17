"""로스트아크 공식 오픈 API 캐릭터 조회 헬퍼."""
import httpx

from core.config import LOSTARK_API_KEY

BASE_URL = "https://developer-lostark.game.onstove.com"


class LostArkAPIError(Exception):
    """캐릭터 조회 실패 시 발생. 메시지는 그대로 사용자에게 보여줘도 되는 문구로 작성함."""


class LostArkCharacterNotFoundError(LostArkAPIError):
    """캐릭터가 실제로 존재하지 않을 때만 발생. (일시적 API 장애 등 다른 오류와 구분해서
    처리해야 하는 경우—예: 강제참여 시 진행 여부 판단—를 위해 별도 타입으로 분리함)"""


async def get_character_basic(character_name: str) -> dict:
    """캐릭터 기본 정보(아이템 레벨, 직업, 서버)를 조회한다.

    Returns: {"level": float, "class_name": str, "server": str}
    실패 시 LostArkAPIError 를 발생시킨다.
    """
    character_name = (character_name or "").strip()
    if not character_name:
        raise LostArkAPIError("캐릭터 닉네임을 입력해주세요.")

    if not LOSTARK_API_KEY:
        print("[로스트아크API] LOSTARK_API_KEY가 비어있어요. .env 파일을 확인해주세요.")
        raise LostArkAPIError("LOSTARK_API_KEY가 설정되어 있지 않아요. 관리자에게 문의해주세요.")

    print(f"[로스트아크API] 키 로드됨 (길이: {len(LOSTARK_API_KEY)}자) - 캐릭터 조회: {character_name}")

    url = f"{BASE_URL}/armories/characters/{character_name}/profiles"
    headers = {
        "accept": "application/json",
        "authorization": f"bearer {LOSTARK_API_KEY}",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise LostArkAPIError(f"로스트아크 API 요청 중 오류가 발생했어요: {e}")

    if resp.history:
        redirected_from = " -> ".join(str(r.url) for r in resp.history) + f" -> {resp.url}"
        print(f"[로스트아크API] 리다이렉트 발생: {redirected_from}")
        # 로스트아크 API는 존재하지 않는 캐릭터를 조회하면 404 JSON 대신
        # /notfound 페이지로 302 리다이렉트시킴. 리다이렉트가 발생했다는 것
        # 자체가 사실상 "캐릭터 없음"이라는 뜻이라 여기서 바로 처리함.
        raise LostArkCharacterNotFoundError(f"'{character_name}' 캐릭터를 찾을 수 없어요. 닉네임을 다시 확인해주세요.")

    if resp.status_code == 404:
        raise LostArkCharacterNotFoundError(f"'{character_name}' 캐릭터를 찾을 수 없어요. 닉네임을 다시 확인해주세요.")
    if resp.status_code == 429:
        raise LostArkAPIError("로스트아크 API 요청이 너무 많아요. 잠시 후 다시 시도해주세요.")
    if resp.status_code in (401, 403):
        print(f"[로스트아크API] 인증 실패 (HTTP {resp.status_code}): {resp.text[:300]}")
        raise LostArkAPIError(
            "로스트아크 API 인증에 실패했어요. .env의 LOSTARK_API_KEY 값에 따옴표나 'bearer ' 접두사가 "
            "같이 들어가있지 않은지, 키 앞뒤에 공백이 없는지 확인해주세요."
        )
    if resp.status_code != 200:
        print(f"[로스트아크API] 오류 응답 (HTTP {resp.status_code}): {resp.text[:300]}")
        raise LostArkAPIError(f"로스트아크 API 오류가 발생했어요. (HTTP {resp.status_code})")

    try:
        data = resp.json()
    except Exception:
        raise LostArkAPIError("로스트아크 API 응답을 해석할 수 없어요.")

    if not data:
        raise LostArkCharacterNotFoundError(f"'{character_name}' 캐릭터를 찾을 수 없어요.")

    try:
        level_str = str(data.get("ItemAvgLevel", "0")).replace(",", "").strip()
        level = float(level_str)
    except Exception:
        level = 0.0

    try:
        combat_power_str = str(data.get("CombatPower", "0")).replace(",", "").strip()
        combat_power = float(combat_power_str)
    except Exception:
        combat_power = 0.0

    return {
        "level": level,
        "combat_power": combat_power,
        "class_name": data.get("CharacterClassName", "") or "",
        "server": data.get("ServerName", "") or "",
    }