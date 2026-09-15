#include "BootActivity.h"

#include <GfxRenderer.h>
#include <I18n.h>

#include "fontIds.h"
#include "images/Logo120.h"

void BootActivity::onEnter() {
  Activity::onEnter();
  RenderLock lock;
  renderer.beginDisplayWork();
  GfxRenderer::ScopedTarget liveUi(renderer, GfxRenderer::FrameOwner::LiveUi);
  if (!liveUi.active()) return;
  renderer.setRenderMode(GfxRenderer::BW);

  const auto pageWidth = renderer.getScreenWidth();
  const auto pageHeight = renderer.getScreenHeight();

  renderer.clearScreen();
  renderer.drawImage(Logo120, (pageWidth - 120) / 2, (pageHeight - 120) / 2, 120, 120);
  renderer.drawCenteredText(UI_10_FONT_ID, pageHeight / 2 + 70, tr(STR_CROSSPOINT), true, EpdFontFamily::BOLD);
  renderer.drawCenteredText(SMALL_FONT_ID, pageHeight / 2 + 95, tr(STR_BOOTING));
  renderer.drawCenteredText(SMALL_FONT_ID, pageHeight - 30, CROSSPOINT_VERSION);
#if FREEINK_DEVICE_PAPERMONO
  renderer.displayBuffer(HalDisplay::FULL_REFRESH);
#else
  renderer.displayBuffer();
#endif
}
