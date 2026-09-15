#pragma once

#include <CrossPointSettings.h>
#include <GfxRenderer.h>
#include <HalGPIO.h>
#include <HalTiltSensor.h>
#include <Logging.h>
#include <components/bars/tap-zones.h>

#include "MappedInputManager.h"
#include "activities/ActivityManager.h"

namespace ReaderUtils {

constexpr unsigned long GO_HOME_MS = 1000;
constexpr unsigned long GO_BACK_OR_HOME_MS = GO_HOME_MS;
constexpr unsigned long SKIP_HOLD_MS = 700;
constexpr unsigned long BOOKMARK_HOLD_MS = 400;
constexpr unsigned long BOOKMARK_MESSAGE_DURATION_MS = 2500;

enum ReaderTouchAction : freeink::ui::ActionId {
  READER_TOUCH_PREV = 1,
  READER_TOUCH_NEXT = 3,
};

inline void applyOrientation(GfxRenderer& renderer, const uint8_t orientation) {
  switch (orientation) {
    case CrossPointSettings::ORIENTATION::PORTRAIT:
      renderer.setOrientation(GfxRenderer::Orientation::Portrait);
      break;
    case CrossPointSettings::ORIENTATION::LANDSCAPE_CW:
      renderer.setOrientation(GfxRenderer::Orientation::LandscapeClockwise);
      break;
    case CrossPointSettings::ORIENTATION::INVERTED:
      renderer.setOrientation(GfxRenderer::Orientation::PortraitInverted);
      break;
    case CrossPointSettings::ORIENTATION::LANDSCAPE_CCW:
      renderer.setOrientation(GfxRenderer::Orientation::LandscapeCounterClockwise);
      break;
    default:
      break;
  }
}

struct PageTurnResult {
  bool prev;
  bool next;
  bool fromTilt;
};

inline PageTurnResult detectPageTurn(const MappedInputManager& input) {
  const bool usePress = SETTINGS.longPressButtonBehavior == SETTINGS.OFF;
  const bool tiltNext = SETTINGS.tiltPageTurn && halTiltSensor.wasTiltedForward();
  const bool tiltPrev = SETTINGS.tiltPageTurn && halTiltSensor.wasTiltedBack();
  const bool swapFront = input.isNavDirectionSwapped();
  const auto prevButton = swapFront ? MappedInputManager::Button::Right : MappedInputManager::Button::Left;
  const auto nextButton = swapFront ? MappedInputManager::Button::Left : MappedInputManager::Button::Right;
  const bool prev =
      tiltPrev ||
      (usePress ? (input.wasPressed(MappedInputManager::Button::PageBack) || input.wasPressed(prevButton))
                : (input.wasReleased(MappedInputManager::Button::PageBack) || input.wasReleased(prevButton)));
  const bool powerTurn = SETTINGS.shortPwrBtn == CrossPointSettings::SHORT_PWRBTN::PAGE_TURN &&
                         input.wasReleased(MappedInputManager::Button::Power);
  const bool next = tiltNext || (usePress ? (input.wasPressed(MappedInputManager::Button::PageForward) || powerTurn ||
                                             input.wasPressed(nextButton))
                                          : (input.wasReleased(MappedInputManager::Button::PageForward) || powerTurn ||
                                             input.wasReleased(nextButton)));
  return {prev, next, tiltPrev || tiltNext};
}

struct TouchPageTurn {
  bool prev;
  bool next;
  unsigned long heldMs;
};

inline TouchPageTurn detectTouchPageTurn(GfxRenderer& renderer, const MappedInputManager& input) {
  TouchPageTurn result{false, false, 0};
  if (!SETTINGS.touchReaderControls || !input.hasTouch()) {
    return result;
  }

  int x = 0;
  int y = 0;

  if (SETTINGS.longPressButtonBehavior == SETTINGS.OFF && input.wasScreenTouchContact(x, y)) {
    const int width = renderer.getScreenWidth();
    const int height = renderer.getScreenHeight();
    if (y > height * 14 / 100 && y < height * 86 / 100) {
      if (x >= width / 3) {
        input.suppressTouchTapOnce();
        result.next = true;
        return result;
      }
      if (x >= width / 4) {
        input.suppressTouchTapOnce();
        result.prev = true;
        return result;
      }
    }
  }

  if (!input.wasScreenTapped(x, y)) {
    return result;
  }

  const int16_t width = static_cast<int16_t>(renderer.getScreenWidth());
  const int16_t height = static_cast<int16_t>(renderer.getScreenHeight());
  const int16_t previousZoneWidth = width / 3;
  const freeink::ui::TapZone zones[] = {
      {freeink::ui::Rect{0, 0, previousZoneWidth, height}, READER_TOUCH_PREV},
      {freeink::ui::Rect{previousZoneWidth, 0, static_cast<int16_t>(width - previousZoneWidth), height},
       READER_TOUCH_NEXT},
  };

  for (const auto& zone : zones) {
    if (!zone.enabled || !zone.rect.contains(static_cast<int16_t>(x), static_cast<int16_t>(y))) continue;
    result.prev = zone.action == READER_TOUCH_PREV;
    result.next = zone.action == READER_TOUCH_NEXT;
    break;
  }
  result.heldMs = gpio.lastTouchHeldMs();
  return result;
}

inline bool isTouchMenuGesture(const MappedInputManager& input) {
  return SETTINGS.touchReaderControls && input.hasTouch() && input.wasMenuGesture();
}

inline bool useBalancedReaderRefresh() {
#if FREEINK_DEVICE_PAPERMONO
  return SETTINGS.readerRefreshMode == CrossPointSettings::READER_REFRESH_BALANCED;
#else
  return true;
#endif
}

inline HalDisplay::RefreshMode refreshModeForCycle(int pagesUntilFullRefresh) {
#if FREEINK_DEVICE_PAPERMONO
  return (pagesUntilFullRefresh <= 1) ? HalDisplay::FULL_REFRESH : HalDisplay::FAST_REFRESH;
#else
  return (pagesUntilFullRefresh <= 1) ? HalDisplay::HALF_REFRESH : HalDisplay::FAST_REFRESH;
#endif
}

inline void advanceRefreshCycle(int& pagesUntilFullRefresh) {
  if (pagesUntilFullRefresh <= 1) {
    pagesUntilFullRefresh = SETTINGS.getRefreshFrequency();
  } else {
    pagesUntilFullRefresh--;
  }
}

inline void displayWithRefreshCycle(const GfxRenderer& renderer, int& pagesUntilFullRefresh, bool async = false) {
  const auto mode = refreshModeForCycle(pagesUntilFullRefresh);
  if (async) {
    renderer.displayBufferAsync(mode);
  } else {
    renderer.displayBuffer(mode);
  }

  if (renderer.displayCommitted()) advanceRefreshCycle(pagesUntilFullRefresh);
}

template <typename RenderFn>
bool renderAntiAliased(GfxRenderer& renderer, int& pagesUntilFullRefresh, int x, int y, int width, int height,
                       RenderFn&& renderFn) {
  if (renderer.displayWorkAborted() || !renderer.liveFrameValid()) return false;
  {
    GfxRenderer::ScopedTarget base(renderer, GfxRenderer::FrameOwner::ReaderBase);
    renderer.displayGrayscaleBase(refreshModeForCycle(pagesUntilFullRefresh));
  }
  {
    GfxRenderer::ScopedTarget scratch(renderer, GfxRenderer::FrameOwner::ReaderScratch);
    if (!scratch.active() || !renderer.storeReaderBwScratch()) {
      LOG_ERR("READER", "Failed to store BW buffer for anti-aliasing");
      return false;
    }
    if (renderer.uiGrayEnabled() && !renderer.beginReaderImport(x, y, width, height)) {
      renderer.restoreReaderBwScratch();
      return false;
    }

    renderer.clearScreen(0x00);
    renderer.setRenderMode(GfxRenderer::GRAYSCALE_LSB);
    renderFn();
    if (renderer.displayWorkAborted()) {
      renderer.restoreReaderBwScratch();
      return false;
    }
    renderer.copyGrayscaleLsbBuffers();

    renderer.clearScreen(0x00);
    renderer.setRenderMode(GfxRenderer::GRAYSCALE_MSB);
    renderFn();
    if (renderer.displayWorkAborted()) {
      renderer.restoreReaderBwScratch();
      return false;
    }
    renderer.copyGrayscaleMsbBuffers();

    if (!renderer.restoreReaderBwScratch() || renderer.displayWorkAborted()) return false;
    if (renderer.uiGrayEnabled() && !renderer.finishReaderImport()) return false;
  }

  GfxRenderer::ScopedTarget base(renderer, GfxRenderer::FrameOwner::ReaderBase);
  renderer.setRenderMode(GfxRenderer::BW);
  renderer.displayGrayBuffer();
  const bool committed = renderer.displayCommitted();
  if (committed) advanceRefreshCycle(pagesUntilFullRefresh);
  if (!renderer.displayWorkAborted()) renderer.cleanupGrayscaleWithFrameBuffer();
  return committed;
}

struct BackNavCallback {
  void* ctx;
  void (*fn)(void*);
};

inline bool handleBackNavigation(const MappedInputManager& mappedInput, ActivityManager& activityManager,
                                 const char* filePath, BackNavCallback goHome) {
  if (mappedInput.isPressed(MappedInputManager::Button::Back) && mappedInput.getHeldTime() >= GO_BACK_OR_HOME_MS) {
    if (SETTINGS.backShortToFileBrowser) {
      goHome.fn(goHome.ctx);
    } else {
      activityManager.goToFileBrowser(filePath);
    }
    return true;
  }
  if (mappedInput.wasReleased(MappedInputManager::Button::Back) && mappedInput.getHeldTime() < GO_BACK_OR_HOME_MS) {
    if (SETTINGS.backShortToFileBrowser) {
      activityManager.goToFileBrowser(filePath);
    } else {
      goHome.fn(goHome.ctx);
    }
    return true;
  }
  return false;
}

}  // namespace ReaderUtils
