import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  LayoutDashboard,
  Database,
  Brain,
  Library,
  TrendingUp,
  Wrench,
  Archive,
  HardDrive,
  Settings
} from 'lucide-react';

const Sidebar: React.FC = () => {
  const location = useLocation();

  const navItems = [
    { path: '/', label: 'Dashboard', icon: LayoutDashboard },
    { path: '/datasets', label: 'Datasets', icon: Database },
    { path: '/training', label: 'Training', icon: Brain },
    { path: '/models', label: 'Models', icon: Library },
    { path: '/backtesting', label: 'Backtesting', icon: TrendingUp },
    { path: '/tools', label: 'Tools', icon: Wrench },
    { path: '/saved-data', label: 'Saved Data', icon: Archive },
    { path: '/cache', label: 'Cache', icon: HardDrive },
    { path: '/settings', label: 'Settings', icon: Settings },
  ];

  return (
    <aside className="w-64 bg-gray-900 text-white h-screen fixed left-0 top-0 overflow-y-auto">
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-8">BA2 Test Platform</h1>
        <nav className="space-y-2">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = location.pathname === item.path;

            return (
              <Link
                key={item.path}
                to={item.path}
                className={`flex items-center space-x-3 px-4 py-3 rounded-lg transition-colors ${
                  isActive
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-300 hover:bg-gray-800 hover:text-white'
                }`}
              >
                <Icon size={20} />
                <span>{item.label}</span>
              </Link>
            );
          })}
        </nav>
      </div>
    </aside>
  );
};

export default Sidebar;
