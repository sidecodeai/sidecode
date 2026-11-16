/**
 * Settings at an address, the way `App` routes it — `/settings/*` and the
 * sections under it — beside whatever pages a test needs to land on, with the
 * router's address written where a test can read it.
 */
import type { ReactNode } from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { SettingsPage } from '../src/features/settings/SettingsPage';
import { Address } from './Address';

export function renderSettings(path = '/settings', pages: ReactNode = null) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/settings/*" element={<SettingsPage />} />
        {pages}
      </Routes>
      <Address />
    </MemoryRouter>,
  );
}

/** Where the router is now: the path and the query. */
export const address = (): string | null => screen.getByTestId('address').textContent;

/** The titles of the rows under `root`, in the order they are drawn. */
export const rowTitles = (root: ParentNode = document): (string | null)[] =>
  [...root.querySelectorAll('.settings-row-title')].map((title) => title.textContent);

/** The captions over the groups on show. */
export const captions = (): (string | null)[] =>
  [...document.querySelectorAll('.settings-content .group-title')].map((title) => title.textContent);

/** The section nav's items, and the one marked current. */
export const navItems = (): (string | null)[] =>
  [...document.querySelectorAll('.settings-nav a')].map((item) => item.textContent);

export const currentNavItem = (): string | null | undefined =>
  document.querySelector('.settings-nav a[aria-current="page"]')?.textContent;

/** The narrow screen's list of sections, by their titles; none where it is not drawn. */
export function listedSections(): (string | null)[] {
  const list = document.querySelector('.settings-list');
  return list ? rowTitles(list) : [];
}

/** The versions line's parts, each drawn whole; the dots between them are the stylesheet's. */
export const versionParts = (): (string | null)[] =>
  [...document.querySelectorAll('.settings-versions .settings-versions-part')].map((part) => part.textContent);
