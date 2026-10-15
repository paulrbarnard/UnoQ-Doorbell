// Doorbell button, MCU side of the UNO Q.
//
// The button goes between IO2 (D2) and GND, using the internal pull-up. On
// each debounced press the sketch sends a `bell_pressed` notification to the
// Linux side (python/doorbell/bridge.py). Everything else (camera, HomeKit,
// pushes) runs on Linux.
//
// Bridge API (arduino-libraries/Arduino_RouterBridge):
//   Bridge.begin()
//   Bridge.notify(name, args...)    fire-and-forget call into Python
//   Bridge.provide_safe(name, fn)   Python -> MCU call, run on the hardware thread
#include <Arduino_RouterBridge.h>

const uint8_t BELL_PIN = 2;
const unsigned long DEBOUNCE_MS = 30;
const unsigned long REPEAT_GUARD_MS = 1000;  // one ring per press, even if the contact bounces long

int stable_level = HIGH;
int last_raw = HIGH;
unsigned long last_change_ms = 0;
unsigned long last_ring_ms = 0;
unsigned long presses = 0;

bool ping() {
  return true;
}

// Lets the web page check the button wiring without ringing.
int bell_state() {
  return digitalRead(BELL_PIN) == LOW ? 1 : 0;
}

void setup() {
  pinMode(BELL_PIN, INPUT_PULLUP);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Bridge.begin();
  Bridge.provide_safe("ping", ping);
  Bridge.provide_safe("bell_state", bell_state);
}

void loop() {
  int raw = digitalRead(BELL_PIN);
  unsigned long now = millis();
  if (raw != last_raw) {
    last_raw = raw;
    last_change_ms = now;
  }
  if (raw != stable_level && now - last_change_ms >= DEBOUNCE_MS) {
    stable_level = raw;
    digitalWrite(LED_BUILTIN, stable_level == LOW ? HIGH : LOW);
    if (stable_level == LOW && now - last_ring_ms >= REPEAT_GUARD_MS) {
      last_ring_ms = now;
      presses++;
      Bridge.notify("bell_pressed", presses);
    }
  }
  delay(2);
}
