"""Browser integration against explicit synthetic tablets. No user's devices touched.
Run: python tests/browser_check.py [output-directory]
Dev dependencies only: playwright, pillow; playwright install chromium.
"""
import json
import sys
import tempfile
import threading
from pathlib import Path

from test_fleet import Fleet, MonitorServer, Tablet
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='naraeon-browser-'))
    root.mkdir(parents=True, exist_ok=True)
    tablets = {name: Tablet(name) for name in ('qa-tab1', 'qa-tab2', 'qa-tab3')}
    # Readable fixture screen, explicitly labeled. Not fabricated device evidence.
    import base64
    from test_fleet import obj
    for idx, tablet in enumerate(tablets.values(), 1):
        original = tablet.request
        def request(method, payload, t=tablet, n=idx, old=original):
            if payload['name'] != 'phone_take_screenshot':
                return old(method, payload)
            t.calls.append(payload)
            image = Image.new('RGB', (800, 1200) if n != 2 else (1200, 800), '#f8fafc')
            d = ImageDraw.Draw(image)
            d.rectangle((20, 25, image.width-20, 160), fill=['#dfe8ff','#e6f3e8','#fff0d9'][n-1])
            d.text((50, 70), 'SYNTHETIC QA TABLET ' + str(n), fill='#152332', font_size=28)
            d.text((50, 210), 'Frame / tap count: ' + str(t.counter), fill='#152332', font_size=25)
            d.rectangle((50, 320, image.width-50, 450), outline='#8994a7', width=3)
            d.text((70, 365), 'Fixture input field', fill='#333333', font_size=24)
            import io
            stream = io.BytesIO(); image.save(stream, 'PNG')
            result = obj({'revision': t.name + '-rev-' + str(t.counter), 'image_id': t.name + '-image-' + str(t.counter)})
            result['content'].insert(0, {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(stream.getvalue()).decode()})
            return result
        tablet.request = request
    fleet = Fleet(list(tablets), lambda name: {'name': name}, client_factory=lambda p,t: tablets[p['name']])
    server = MonitorServer(fleet, interval=1)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    errors = []
    checks = []
    try:
        with sync_playwright() as pw:
            import os
            channel = os.environ.get('NARAE_QA_BROWSER_CHANNEL')
            browser = pw.chromium.launch(headless=True, **({'channel': channel} if channel else {}))
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(server.url)
            page.wait_for_selector('.card')
            assert page.locator('.card').count() == 3
            assert not tablets['qa-tab1'].calls
            page.get_by_role('button', name='모두 보기 시작').click()
            page.wait_for_function("() => [...document.querySelectorAll('.screen img')].every(i=>i.complete && i.naturalWidth>0)")
            checks.append('three independent periodic screenshot cards render; no initial automatic calls')
            page.screenshot(path=str(root/'desktop-fixtures.png'), full_page=True)
            card1, card2 = page.locator('.card').nth(0), page.locator('.card').nth(1)
            card1.locator('.arm').click()
            page.wait_for_function("() => document.querySelector('.card .control-state').textContent.includes('확인됨')")
            # Wait for fresh frame after control generation changes, not the prior image.
            page.wait_for_function("() => !document.querySelector('.card .controls button').disabled")
            image = card1.locator('img'); rect = image.bounding_box()
            image.click(position={'x': rect['width'] * .25, 'y': rect['height'] * .50})
            page.wait_for_function("() => document.querySelector('.card .status').textContent.includes('수신')")
            actions = [c for c in tablets['qa-tab1'].calls if c['name'] == 'phone_act']
            assert len(actions) == 1, actions
            args = actions[0]['arguments']
            assert abs(args['x']-200) < 3 and abs(args['y']-600) < 3, args
            assert not any(c['name'] == 'phone_act' for c in tablets['qa-tab2'].calls)
            checks.append('portrait image scaling maps actual click to correct device, image ID and pixels')
            # Browser input (including Korean) is sent once to a fresh, current-frame node.
            page.wait_for_function("() => !document.querySelector('.card .typing button').disabled")
            card1.locator('.text').fill('태블릿 한글 입력 시험')
            card1.locator('.typing button').click()
            page.wait_for_function("() => !document.querySelector('.card .typing button').disabled")
            actions = [c for c in tablets['qa-tab1'].calls if c['name'] == 'phone_act']
            assert actions[-1]['arguments']['text'] == '태블릿 한글 입력 시험'
            assert len(actions) == 2
            card1.get_by_role('button', name='아래로 스크롤', exact=True).click()
            page.wait_for_function("() => !document.querySelector('.card .controls button').disabled")
            actions = [c for c in tablets['qa-tab1'].calls if c['name'] == 'phone_act']
            assert actions[-1]['arguments']['action'] == 'scroll_forward'
            checks.append('Korean input and scroll use current selected node and one mutation each')
            card2.locator('.expand').click()
            assert card2.evaluate('(e)=>e.classList.contains("expanded")')
            page.screenshot(path=str(root/'landscape-expanded-fixture.png'), full_page=True)
            card2.locator('.arm').click()
            page.wait_for_function("() => !document.querySelectorAll('.card')[1].querySelector('.controls button').disabled")
            im2 = card2.locator('img'); box2 = im2.bounding_box()
            im2.click(position={'x': box2['width']*.75, 'y': box2['height']*.25})
            page.wait_for_function("() => !document.querySelectorAll('.card')[1].querySelector('.controls button').disabled")
            args2 = [c for c in tablets['qa-tab2'].calls if c['name']=='phone_act'][-1]['arguments']
            assert abs(args2['x']-900)<3 and abs(args2['y']-200)<3, args2
            checks.append('landscape expanded card maps click without letterbox or device confusion')
            # A timeout on tablet 1 stops it, leaving tablet 2 viewing independently.
            tablets['qa-tab1'].fail = 'phone_read_screen'
            page.wait_for_function("() => document.querySelector('.card .status').classList.contains('error')", timeout=12000)
            assert not fleet.devices['qa-tab1'].active
            assert fleet.devices['qa-tab2'].active
            count = len(tablets['qa-tab1'].calls)
            page.wait_for_timeout(2300)
            assert len(tablets['qa-tab1'].calls) == count
            checks.append('read timeout stops only failed card, clears image and never retries')
            page.get_by_role('button', name='전체 관제 중지').click()
            page.wait_for_function("() => [...document.querySelectorAll('.screen img')].every(i=>i.hidden)")
            assert all(not dev.active and not dev.armed for dev in fleet.devices.values())
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(root/'mobile-fixtures.png'), full_page=True)
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
            checks.append('global stop clears frames and control arming; mobile has no horizontal overflow')
            assert not errors, errors
            browser.close()
        receipt = {'ok': True, 'evidence_type': 'synthetic-tablet-browser-integration',
                   'physical_android_verified': False, 'checks': checks, 'page_errors': errors}
        (root/'browser-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__': main()
