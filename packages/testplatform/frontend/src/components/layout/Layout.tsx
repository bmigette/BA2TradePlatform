import React from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';

const Layout: React.FC = () => {
  return (
    <div className="flex min-h-screen bg-gray-100 dark:bg-gray-950">
      <Sidebar />
      <main className="flex-1 ml-64 min-w-0 overflow-x-hidden">
        <Outlet />
      </main>
    </div>
  );
};

export default Layout;
